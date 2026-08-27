"""Provider credentials, read from MySQL instead of a file on the server.

The dotenv resolver already defines what a KIS, Toss or Binance credential is,
and those types are what the adapters take. This returns exactly those, so
moving the source of truth is a change of where the bytes come from and not a
change of what anything downstream sees.

The names are built rather than free text. A credential filed under a name
somebody typed differently the second time is a credential that silently is
not there, and the resolver would report the account as unconfigured rather
than as misconfigured.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.secrets import (
    ACCOUNT_IDENTIFIER,
    PROVIDER_CREDENTIAL,
    MySqlSecretStore,
    SecretNotFoundError,
    SecretScope,
)
from autotrader.config.account_secrets import (
    DB_KIS_PAPER_REFERENCE,
    DB_KIS_REAL_REFERENCE,
    DB_TOSS_LIVE_REFERENCE,
    AccountSecretResolutionError,
    KisAccountSecret,
    TossAccountSecret,
)
from autotrader.integrations.brokers.binance_usdm.secrets import BinanceUsdmSecret
from autotrader.security.secret_crypto import MasterKeyRing

KIS = "KIS"
TOSS = "TOSS"
BINANCE = "BINANCE"
LIVE = "LIVE"
PAPER = "PAPER"

# The reference an adapter is configured with. The credential types own these
# strings, because the reference is part of what makes a credential valid and
# part of the account scope hash.
KIS_LIVE_REFERENCE = DB_KIS_REAL_REFERENCE
KIS_PAPER_REFERENCE = DB_KIS_PAPER_REFERENCE
TOSS_LIVE_REFERENCE = DB_TOSS_LIVE_REFERENCE
BINANCE_LIVE_REFERENCE = "secret://db/binance/usdm/live"

_IDENTIFIER_FIELDS = frozenset({"account-number", "product-code"})


@dataclass(frozen=True, slots=True)
class ProviderField:
    """One stored value, and where it belongs."""

    provider: str
    environment: str
    field: str

    def __post_init__(self) -> None:
        if self.provider not in (KIS, TOSS, BINANCE):
            raise ValueError("unknown provider")
        if self.environment not in (LIVE, PAPER):
            raise ValueError("environment is LIVE or PAPER")
        if not self.field or self.field != self.field.strip().lower():
            raise ValueError("a field name is lowercase and trimmed")

    @property
    def logical_name(self) -> str:
        """Built from the scope, so it cannot be typed differently twice."""
        return f"{self.provider.lower()}-{self.environment.lower()}-{self.field}"

    @property
    def scope(self) -> SecretScope:
        return SecretScope(
            category=(
                ACCOUNT_IDENTIFIER
                if self.field in _IDENTIFIER_FIELDS
                else PROVIDER_CREDENTIAL
            ),
            provider_code=self.provider,
            environment=self.environment,
        )

    @property
    def reference(self) -> str:
        return f"secret://db/{self.logical_name}@active"


KIS_FIELDS = ("app-key", "app-secret", "account-number", "product-code")
TOSS_FIELDS = ("client-id", "client-secret")
BINANCE_FIELDS = ("api-key", "secret-key")


def fields_for(provider: str, environment: str) -> tuple[ProviderField, ...]:
    """Every value a provider needs, so a partial set is visible as one."""
    names = {KIS: KIS_FIELDS, TOSS: TOSS_FIELDS, BINANCE: BINANCE_FIELDS}[provider]
    return tuple(
        ProviderField(provider=provider, environment=environment, field=name)
        for name in names
    )


class MySqlAccountSecretResolver:
    """The database counterpart of DotenvAccountSecretResolver."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], keys: MasterKeyRing
    ) -> None:
        self._store = MySqlSecretStore(sessions, keys)

    async def resolve_kis(self, reference: str) -> KisAccountSecret:
        environment = {
            KIS_LIVE_REFERENCE: LIVE,
            KIS_PAPER_REFERENCE: PAPER,
        }.get(reference)
        if environment is None:
            raise AccountSecretResolutionError("account secret is unavailable")
        values = await self._values(KIS, environment, KIS_FIELDS)
        return KisAccountSecret(
            reference=reference,
            environment=environment,
            app_key=SecretStr(values["app-key"]),
            app_secret=SecretStr(values["app-secret"]),
            account_number=SecretStr(values["account-number"]),
            product_code=values["product-code"],
        )

    async def resolve_toss(self, reference: str) -> TossAccountSecret:
        if reference != TOSS_LIVE_REFERENCE:
            raise AccountSecretResolutionError("account secret is unavailable")
        values = await self._values(TOSS, LIVE, TOSS_FIELDS)
        return TossAccountSecret(
            reference=reference,
            environment=LIVE,
            client_id=SecretStr(values["client-id"]),
            client_secret=SecretStr(values["client-secret"]),
        )

    async def resolve_binance_usdm(self, reference: str) -> BinanceUsdmSecret:
        if reference != BINANCE_LIVE_REFERENCE:
            raise AccountSecretResolutionError("account secret is unavailable")
        values = await self._values(BINANCE, LIVE, BINANCE_FIELDS)
        return BinanceUsdmSecret(
            api_key=SecretStr(values["api-key"]),
            secret_key=SecretStr(values["secret-key"]),
        )

    async def _values(
        self, provider: str, environment: str, names: tuple[str, ...]
    ) -> dict[str, str]:
        """All of them, or none.

        Half a credential set is not a usable account, and returning what was
        found would push the failure into a signing routine where it reads as
        a rejected request rather than as missing configuration.
        """
        collected: dict[str, str] = {}
        for name in names:
            field = ProviderField(
                provider=provider, environment=environment, field=name
            )
            try:
                secret = await self._store.resolve(field.reference)
            except SecretNotFoundError as error:
                # The same message whichever field is missing: which one it is
                # belongs in the operator's screen, not in an exception that
                # may be logged somewhere less careful.
                raise AccountSecretResolutionError(
                    "account secret is unavailable"
                ) from error
            collected[name] = secret.reveal()
        return collected


__all__ = (
    "BINANCE",
    "BINANCE_FIELDS",
    "BINANCE_LIVE_REFERENCE",
    "KIS",
    "KIS_FIELDS",
    "KIS_LIVE_REFERENCE",
    "KIS_PAPER_REFERENCE",
    "LIVE",
    "PAPER",
    "TOSS",
    "TOSS_FIELDS",
    "TOSS_LIVE_REFERENCE",
    "MySqlAccountSecretResolver",
    "ProviderField",
    "fields_for",
)
