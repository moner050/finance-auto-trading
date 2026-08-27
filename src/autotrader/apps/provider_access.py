"""Where an adapter gets its credentials, one call at a time.

The adapters take call-scoped credentials and say plainly that callers must
not persist them. Nothing built them, so this does: it resolves the stored
secret, and for KIS it also holds the access token that the credential needs.

The token cache is the part worth reading carefully. KIS returns an expiry as
a bare "YYYY-MM-DD HH:MM:SS" with no zone, and it means Seoul. Reading that
wrongly by nine hours in the wrong direction would mean presenting an expired
token for most of a day, so nothing here depends on getting it right: the
cache lifetime is measured from issuance on our own clock, and the provider's
expiry can only shorten it. A misread makes us refresh early, never late.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from autotrader.config.account_secrets import (
    KisAccountSecret,
    TossAccountSecret,
)
from autotrader.integrations.brokers.binance_usdm.secrets import BinanceUsdmSecret
from autotrader.integrations.brokers.kis.oauth import (
    KisAccessToken,
    KisClientCredentials,
)
from autotrader.integrations.brokers.kis.read_contracts import KisReadCredentials
from autotrader.integrations.brokers.toss.market_data_reader import (
    TossClientCredentials,
)
from autotrader.shared.time import require_utc

# KIS tokens last a day. Six hours keeps a working session on one token while
# leaving a wide margin for a clock or a zone read the wrong way.
KIS_TOKEN_LIFETIME = timedelta(hours=6)
KIS_EXPIRY_MARGIN = timedelta(minutes=5)
KIS_EXPIRY_ZONE = ZoneInfo("Asia/Seoul")
_KIS_EXPIRY_FORMAT = "%Y-%m-%d %H:%M:%S"


class AccountSecretSource(Protocol):
    """Either resolver: the database one, or the dotenv one."""

    async def resolve_kis(self, reference: str) -> KisAccountSecret: ...

    async def resolve_toss(self, reference: str) -> TossAccountSecret: ...

    async def resolve_binance_usdm(self, reference: str) -> BinanceUsdmSecret: ...


class KisTokenIssuer(Protocol):
    async def issue_access_token(
        self, *, credentials: KisClientCredentials
    ) -> KisAccessToken: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class HeldToken:
    """One token, and the moment after which it stops being offered."""

    token: KisAccessToken
    usable_until: datetime

    def usable_at(self, moment: datetime) -> bool:
        return moment < self.usable_until


def token_deadline(token: KisAccessToken, *, issued_at: datetime) -> datetime:
    """The earlier of our own lifetime and what the provider said.

    Taking the earlier of the two is what makes the zone reading harmless. If
    Seoul is the wrong guess the parsed expiry lands somewhere useless, and
    the lifetime measured from issuance is still there underneath it.
    """
    moment = require_utc(issued_at)
    ours = moment + KIS_TOKEN_LIFETIME
    theirs = (
        datetime.strptime(token.expires_at_raw, _KIS_EXPIRY_FORMAT)
        .replace(tzinfo=KIS_EXPIRY_ZONE)
        .astimezone(moment.tzinfo)
        - KIS_EXPIRY_MARGIN
    )
    return min(ours, theirs)


class ProviderAccess:
    """Call-scoped credentials, built when a call needs them."""

    def __init__(
        self,
        *,
        secrets: AccountSecretSource,
        kis_tokens: KisTokenIssuer,
        clock: Clock,
    ) -> None:
        self._secrets = secrets
        self._kis_tokens = kis_tokens
        self._clock = clock
        self._held: dict[str, HeldToken] = {}

    async def kis(self, reference: str) -> KisReadCredentials:
        secret = await self._secrets.resolve_kis(reference)
        app_key = secret.app_key.get_secret_value()
        app_secret = secret.app_secret.get_secret_value()
        token = await self._token(
            reference, KisClientCredentials(app_key=app_key, app_secret=app_secret)
        )
        return KisReadCredentials(
            access_token=token.value, app_key=app_key, app_secret=app_secret
        )

    async def toss(self, reference: str) -> TossClientCredentials:
        secret = await self._secrets.resolve_toss(reference)
        return TossClientCredentials(
            client_id=secret.client_id.get_secret_value(),
            client_secret=secret.client_secret.get_secret_value(),
        )

    async def binance_usdm(self, reference: str) -> BinanceUsdmSecret:
        return await self._secrets.resolve_binance_usdm(reference)

    async def _token(
        self, reference: str, credentials: KisClientCredentials
    ) -> KisAccessToken:
        moment = require_utc(self._clock.now())
        held = self._held.get(reference)
        if held is not None and held.usable_at(moment):
            return held.token
        issued = await self._kis_tokens.issue_access_token(credentials=credentials)
        # Kept per reference, so a paper token can never be presented to the
        # live endpoint or the other way round.
        self._held[reference] = HeldToken(
            token=issued, usable_until=token_deadline(issued, issued_at=moment)
        )
        return issued

    def forget(self, reference: str | None = None) -> None:
        """Drop held tokens, which a rotated credential invalidates."""
        if reference is None:
            self._held.clear()
        else:
            self._held.pop(reference, None)


__all__ = (
    "KIS_EXPIRY_MARGIN",
    "KIS_EXPIRY_ZONE",
    "KIS_TOKEN_LIFETIME",
    "AccountSecretSource",
    "Clock",
    "HeldToken",
    "KisTokenIssuer",
    "ProviderAccess",
    "token_deadline",
)
