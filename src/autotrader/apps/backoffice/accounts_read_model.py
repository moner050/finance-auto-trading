"""What the accounts screen shows about a credential: that it is there.

Section 11.2 draws the line in four words — secret availability and
fingerprint only. So this reports whether each value a provider needs is
stored and a short prefix of its fingerprint, and it does that without
decrypting anything. The fingerprint comes off the stored row; the plaintext
is never asked for, so there is no moment at which it exists to be leaked.

A provider with some of its values reads as incomplete rather than as ready,
because that is what it is, and an operator seeing "ready" on an account that
cannot sign a request is worse than seeing nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.provider_secrets import (
    BINANCE,
    KIS,
    TOSS,
    ProviderField,
    fields_for,
)
from autotrader.apps.backoffice.secrets import (
    MySqlSecretStore,
    SecretNotFoundError,
)
from autotrader.persistence.mysql.models.accounts import Account, Broker
from autotrader.persistence.mysql.models.risk import RiskPolicyVersion
from autotrader.security.secret_crypto import MasterKeyRing

# Enough to tell two fingerprints apart on a screen, and not the fingerprint.
FINGERPRINT_PREFIX = 8


@dataclass(frozen=True, slots=True)
class StoredValueView:
    """One credential value, named but never shown."""

    logical_name: str
    present: bool
    fingerprint_prefix: str | None


@dataclass(frozen=True, slots=True)
class ProviderCredentialsView:
    provider: str
    environment: str
    values: tuple[StoredValueView, ...]

    @property
    def complete(self) -> bool:
        """All of them, because some of them is not a usable account."""
        return bool(self.values) and all(value.present for value in self.values)

    @property
    def stored_count(self) -> int:
        return sum(1 for value in self.values if value.present)


@dataclass(frozen=True, slots=True)
class AccountView:
    account_alias: str
    broker_code: str
    environment: str
    enabled: bool
    # The reference, which names where the credentials live. Not a credential.
    secret_reference: str


@dataclass(frozen=True, slots=True)
class AccountsView:
    accounts: tuple[AccountView, ...]
    credentials: tuple[ProviderCredentialsView, ...]
    active_policy_version: str | None


class AccountsReadModel:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], keys: MasterKeyRing
    ) -> None:
        self._sessions = sessions
        self._store = MySqlSecretStore(sessions, keys)

    async def load(self) -> AccountsView:
        return AccountsView(
            accounts=await self.accounts(),
            credentials=await self.credentials(),
            active_policy_version=await self.active_policy_version(),
        )

    async def accounts(self) -> tuple[AccountView, ...]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(Account, Broker.code)
                    .join(Broker, Broker.id == Account.broker_id)
                    .order_by(Broker.code, Account.account_alias)
                )
            ).all()
        return tuple(
            AccountView(
                account_alias=account.account_alias,
                broker_code=code,
                environment=account.environment,
                enabled=account.enabled,
                secret_reference=account.secret_reference,
            )
            for account, code in rows
        )

    async def credentials(self) -> tuple[ProviderCredentialsView, ...]:
        collected: list[ProviderCredentialsView] = []
        for provider, environment in (
            (KIS, "PAPER"),
            (KIS, "LIVE"),
            (TOSS, "LIVE"),
            (BINANCE, "LIVE"),
        ):
            collected.append(
                ProviderCredentialsView(
                    provider=provider,
                    environment=environment,
                    values=tuple(
                        [
                            await self._value(field)
                            for field in fields_for(provider, environment)
                        ]
                    ),
                )
            )
        return tuple(collected)

    async def active_policy_version(self) -> str | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(RiskPolicyVersion.version)
                .where(RiskPolicyVersion.active.is_(True))
                .order_by(RiskPolicyVersion.id.desc())
                .limit(1)
            )

    async def _value(self, field: ProviderField) -> StoredValueView:
        try:
            fingerprint = await self._store.fingerprint(field.reference)
        except SecretNotFoundError:
            # Absent is a fact worth showing, not an error worth raising: a
            # screen that fails because one credential is missing cannot be
            # used to find out which one.
            return StoredValueView(
                logical_name=field.logical_name,
                present=False,
                fingerprint_prefix=None,
            )
        return StoredValueView(
            logical_name=field.logical_name,
            present=True,
            fingerprint_prefix=fingerprint.hex()[:FINGERPRINT_PREFIX],
        )


__all__ = (
    "FINGERPRINT_PREFIX",
    "AccountView",
    "AccountsReadModel",
    "AccountsView",
    "ProviderCredentialsView",
    "StoredValueView",
)
