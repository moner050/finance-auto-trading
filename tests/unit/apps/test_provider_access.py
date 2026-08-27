"""Building call-scoped credentials, and holding a KIS token safely.

The token cache is the part that can go quietly wrong, so most of this is
about when a held token stops being offered.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from autotrader.apps.provider_access import (
    KIS_TOKEN_LIFETIME,
    ProviderAccess,
    token_deadline,
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
from autotrader.integrations.brokers.kis.oauth import (
    KisAccessToken,
    KisClientCredentials,
)

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
BINANCE_REFERENCE = "secret://db/binance/usdm/live"


def _kis(reference: str, app_key: str = "an-app-key") -> KisAccountSecret:
    return KisAccountSecret(
        reference=reference,
        environment="PAPER" if "paper" in reference else "LIVE",
        app_key=SecretStr(app_key),
        app_secret=SecretStr("an-app-secret"),
        account_number=SecretStr("12345678"),
        product_code="01",
    )


class _Secrets:
    def __init__(self) -> None:
        self.kis_calls: list[str] = []

    async def resolve_kis(self, reference: str) -> KisAccountSecret:
        self.kis_calls.append(reference)
        if reference not in (DB_KIS_REAL_REFERENCE, DB_KIS_PAPER_REFERENCE):
            raise AccountSecretResolutionError("account secret is unavailable")
        key = "live-key" if reference == DB_KIS_REAL_REFERENCE else "paper-key"
        return _kis(reference, key)

    async def resolve_toss(self, reference: str) -> TossAccountSecret:
        return TossAccountSecret(
            reference=reference,
            environment="LIVE",
            client_id=SecretStr("a-client-id"),
            client_secret=SecretStr("a-client-secret"),
        )

    async def resolve_binance_usdm(self, reference: str) -> BinanceUsdmSecret:
        del reference
        return BinanceUsdmSecret(
            api_key=SecretStr("an-api-key"), secret_key=SecretStr("a-secret-key")
        )


class _Issuer:
    def __init__(self, expires_at_raw: str = "2026-08-28 21:00:00") -> None:
        self._expires_at_raw = expires_at_raw
        self.issued: list[str] = []

    async def issue_access_token(
        self, *, credentials: KisClientCredentials
    ) -> KisAccessToken:
        self.issued.append(credentials.app_key)
        return KisAccessToken(
            value=f"token-for-{credentials.app_key}",
            expires_at_raw=self._expires_at_raw,
        )


class _Clock:
    def __init__(self, moment: datetime = NOW) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment


def _access(
    secrets: _Secrets | None = None,
    issuer: _Issuer | None = None,
    clock: _Clock | None = None,
) -> ProviderAccess:
    return ProviderAccess(
        secrets=secrets or _Secrets(),
        kis_tokens=issuer or _Issuer(),
        clock=clock or _Clock(),
    )


@pytest.mark.asyncio
async def test_kis_credentials_carry_the_stored_key_and_a_fresh_token() -> None:
    credentials = await _access().kis(DB_KIS_PAPER_REFERENCE)

    assert credentials.app_key == "paper-key"
    assert credentials.access_token == "token-for-paper-key"


@pytest.mark.asyncio
async def test_a_held_token_is_reused_rather_than_reissued() -> None:
    issuer = _Issuer()
    access = _access(issuer=issuer)

    await access.kis(DB_KIS_PAPER_REFERENCE)
    await access.kis(DB_KIS_PAPER_REFERENCE)

    assert issuer.issued == ["paper-key"]


@pytest.mark.asyncio
async def test_paper_and_live_never_share_a_token() -> None:
    """Presenting one to the other endpoint is the mistake worth preventing."""
    issuer = _Issuer()
    access = _access(issuer=issuer)

    paper = await access.kis(DB_KIS_PAPER_REFERENCE)
    live = await access.kis(DB_KIS_REAL_REFERENCE)

    assert paper.access_token != live.access_token
    assert issuer.issued == ["paper-key", "live-key"]


@pytest.mark.asyncio
async def test_a_token_is_reissued_once_its_lifetime_is_up() -> None:
    issuer, clock = _Issuer(), _Clock()
    access = _access(issuer=issuer, clock=clock)
    await access.kis(DB_KIS_PAPER_REFERENCE)

    clock.moment = NOW + KIS_TOKEN_LIFETIME
    await access.kis(DB_KIS_PAPER_REFERENCE)

    assert len(issuer.issued) == 2


@pytest.mark.asyncio
async def test_rotating_a_credential_drops_the_token_held_for_it() -> None:
    issuer = _Issuer()
    access = _access(issuer=issuer)
    await access.kis(DB_KIS_PAPER_REFERENCE)

    access.forget(DB_KIS_PAPER_REFERENCE)
    await access.kis(DB_KIS_PAPER_REFERENCE)

    assert len(issuer.issued) == 2


def test_the_provider_expiry_can_only_shorten_our_own_lifetime() -> None:
    """Nothing depends on reading the zone correctly, because the earlier of
    the two always wins."""
    soon = KisAccessToken(value="t", expires_at_raw="2026-08-27 22:00:00")
    far = KisAccessToken(value="t", expires_at_raw="2030-01-01 00:00:00")

    # Seoul 22:00 is 13:00 UTC, an hour away, which is inside our six.
    assert token_deadline(soon, issued_at=NOW) < NOW + KIS_TOKEN_LIFETIME
    # An expiry years out cannot extend it.
    assert token_deadline(far, issued_at=NOW) == NOW + KIS_TOKEN_LIFETIME


def test_an_expiry_already_past_yields_a_deadline_already_past() -> None:
    stale = KisAccessToken(value="t", expires_at_raw="2020-01-01 00:00:00")

    assert token_deadline(stale, issued_at=NOW) < NOW


@pytest.mark.asyncio
async def test_a_token_the_provider_says_is_spent_is_not_offered_again() -> None:
    issuer = _Issuer(expires_at_raw="2020-01-01 00:00:00")
    access = _access(issuer=issuer)

    await access.kis(DB_KIS_PAPER_REFERENCE)
    await access.kis(DB_KIS_PAPER_REFERENCE)

    assert len(issuer.issued) == 2


@pytest.mark.asyncio
async def test_toss_credentials_are_built_from_the_stored_values() -> None:
    credentials = await _access().toss(DB_TOSS_LIVE_REFERENCE)

    assert credentials.client_id == "a-client-id"
    assert credentials.client_secret == "a-client-secret"


@pytest.mark.asyncio
async def test_binance_comes_back_as_the_secret_type() -> None:
    secret = await _access().binance_usdm(BINANCE_REFERENCE)

    assert secret.api_key.get_secret_value() == "an-api-key"


@pytest.mark.asyncio
async def test_an_unavailable_secret_never_reaches_the_token_endpoint() -> None:
    issuer = _Issuer()
    access = _access(issuer=issuer)

    with pytest.raises(AccountSecretResolutionError):
        await access.kis("secret://db/kis/nowhere")

    # Asking a provider to authenticate credentials nobody has is a request
    # worth not making.
    assert issuer.issued == []


@pytest.mark.asyncio
async def test_forgetting_everything_drops_every_held_token() -> None:
    issuer = _Issuer()
    access = _access(issuer=issuer)
    await access.kis(DB_KIS_PAPER_REFERENCE)
    await access.kis(DB_KIS_REAL_REFERENCE)

    access.forget()
    await access.kis(DB_KIS_PAPER_REFERENCE)
    await access.kis(DB_KIS_REAL_REFERENCE)

    assert len(issuer.issued) == 4


def test_a_deadline_needs_an_aware_moment() -> None:
    token = KisAccessToken(value="t", expires_at_raw="2026-08-28 21:00:00")

    with pytest.raises(ValueError):
        token_deadline(token, issued_at=datetime(2026, 8, 27, 12, 0))
