"""Provider credentials in MySQL, and what the adapters get back.

The point of moving the source of truth is that nothing downstream notices.
So these check the exact types the dotenv resolver already returns, and check
that a partial set reads as an unconfigured account rather than half of one.
"""

from __future__ import annotations

import asyncio
from base64 import b64encode
from datetime import UTC, datetime

import pytest
from conftest import integration_database_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.bootstrap import master_key_ring
from autotrader.apps.backoffice.credentials import store_set
from autotrader.apps.backoffice.provider_secrets import (
    BINANCE,
    BINANCE_LIVE_REFERENCE,
    KIS,
    KIS_LIVE_REFERENCE,
    KIS_PAPER_REFERENCE,
    LIVE,
    PAPER,
    TOSS,
    TOSS_LIVE_REFERENCE,
    MySqlAccountSecretResolver,
    fields_for,
)
from autotrader.apps.backoffice.secrets import MySqlSecretStore
from autotrader.config.account_secrets import (
    AccountSecretResolutionError,
    KisAccountSecret,
    TossAccountSecret,
)
from autotrader.config.settings import Settings
from autotrader.integrations.brokers.binance_usdm.secrets import (
    BinanceUsdmSecret,
    binance_usdm_api_key_fingerprint,
)
from autotrader.persistence.mysql.engine import create_engine

NOW = datetime(2026, 8, 27, tzinfo=UTC)
KEY = b64encode(b"k" * 32).decode()

KIS_VALUES = {
    "app-key": "a-kis-app-key",
    "app-secret": "a-kis-app-secret",
    "account-number": "12345678",
    "product-code": "01",
}
TOSS_VALUES = {"client-id": "a-toss-client", "client-secret": "a-toss-secret"}
BINANCE_VALUES = {"api-key": "a-binance-key", "secret-key": "a-binance-secret"}


def _settings(url: str) -> Settings:
    return Settings(
        database_url=url,
        backoffice_public_url="https://backoffice.example.com",
        backoffice_master_key=KEY,  # type: ignore[arg-type]
        backoffice_master_key_version=1,
    )


def _drive(scenario: object) -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")

    async def run() -> None:
        settings = _settings(url)
        engine = create_engine(settings)
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await scenario(settings, sessions)  # type: ignore[operator]
        finally:
            await engine.dispose()

    asyncio.run(run())


def _resolver(
    settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> MySqlAccountSecretResolver:
    return MySqlAccountSecretResolver(sessions, master_key_ring(settings))


@pytest.mark.integration
def test_a_kis_set_comes_back_as_the_type_the_adapters_take() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await store_set(fields_for(KIS, PAPER), KIS_VALUES, settings=settings)

        secret = await _resolver(settings, sessions).resolve_kis(KIS_PAPER_REFERENCE)

        assert isinstance(secret, KisAccountSecret)
        assert secret.environment == PAPER
        assert secret.app_key.get_secret_value() == "a-kis-app-key"
        assert secret.account_number.get_secret_value() == "12345678"
        assert secret.product_code == "01"

    _drive(scenario)


@pytest.mark.integration
def test_paper_and_live_credentials_do_not_reach_each_other() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await store_set(fields_for(KIS, PAPER), KIS_VALUES, settings=settings)
        await store_set(
            fields_for(KIS, LIVE),
            KIS_VALUES | {"app-key": "the-live-key"},
            settings=settings,
        )
        resolver = _resolver(settings, sessions)

        paper = await resolver.resolve_kis(KIS_PAPER_REFERENCE)
        live = await resolver.resolve_kis(KIS_LIVE_REFERENCE)

        # The one mistake this naming exists to prevent.
        assert paper.app_key.get_secret_value() == "a-kis-app-key"
        assert live.app_key.get_secret_value() == "the-live-key"

    _drive(scenario)


@pytest.mark.integration
def test_a_toss_set_comes_back_as_the_type_the_adapters_take() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await store_set(fields_for(TOSS, LIVE), TOSS_VALUES, settings=settings)

        secret = await _resolver(settings, sessions).resolve_toss(TOSS_LIVE_REFERENCE)

        assert isinstance(secret, TossAccountSecret)
        assert secret.client_secret.get_secret_value() == "a-toss-secret"

    _drive(scenario)


@pytest.mark.integration
def test_a_binance_set_comes_back_with_a_usable_fingerprint() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await store_set(fields_for(BINANCE, LIVE), BINANCE_VALUES, settings=settings)

        secret = await _resolver(settings, sessions).resolve_binance_usdm(
            BINANCE_LIVE_REFERENCE
        )

        assert isinstance(secret, BinanceUsdmSecret)
        # What may be recorded about the key, rather than the key.
        assert len(binance_usdm_api_key_fingerprint(secret)) == 32

    _drive(scenario)


@pytest.mark.integration
def test_a_partial_set_reads_as_an_unconfigured_account() -> None:
    """Not as an account with some values missing. Half a credential set is
    not usable, and finding out inside a signing routine would read as a
    rejected request rather than as missing configuration."""

    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        store = MySqlSecretStore(sessions, master_key_ring(settings))
        partial = fields_for(KIS, PAPER)[0]
        await store.store(
            logical_name=partial.logical_name,
            scope=partial.scope,
            plaintext="a-kis-app-key",
            now=NOW,
        )

        with pytest.raises(AccountSecretResolutionError):
            await _resolver(settings, sessions).resolve_kis(KIS_PAPER_REFERENCE)

    _drive(scenario)


@pytest.mark.integration
def test_a_reference_for_another_provider_is_refused() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await store_set(fields_for(TOSS, LIVE), TOSS_VALUES, settings=settings)
        resolver = _resolver(settings, sessions)

        with pytest.raises(AccountSecretResolutionError):
            await resolver.resolve_toss(KIS_PAPER_REFERENCE)
        with pytest.raises(AccountSecretResolutionError):
            await resolver.resolve_kis(TOSS_LIVE_REFERENCE)

    _drive(scenario)


@pytest.mark.integration
def test_nothing_stored_is_a_refusal_not_an_empty_credential() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        with pytest.raises(AccountSecretResolutionError):
            await _resolver(settings, sessions).resolve_kis(KIS_PAPER_REFERENCE)

    _drive(scenario)


@pytest.mark.integration
def test_rotating_one_value_keeps_the_set_whole() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await store_set(fields_for(TOSS, LIVE), TOSS_VALUES, settings=settings)
        await store_set(
            fields_for(TOSS, LIVE),
            TOSS_VALUES | {"client-secret": "a-rotated-secret"},
            settings=settings,
        )

        secret = await _resolver(settings, sessions).resolve_toss(TOSS_LIVE_REFERENCE)

        assert secret.client_secret.get_secret_value() == "a-rotated-secret"
        assert secret.client_id.get_secret_value() == "a-toss-client"

    _drive(scenario)


@pytest.mark.integration
def test_an_incomplete_store_leaves_nothing_behind() -> None:
    """The whole set is one transaction, so a value the column refuses takes
    the rest of the set with it."""

    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        with pytest.raises(Exception):  # noqa: B017
            await store_set(
                fields_for(TOSS, LIVE),
                {"client-id": "a-toss-client", "client-secret": "x" * 20000},
                settings=settings,
            )

        with pytest.raises(AccountSecretResolutionError):
            await _resolver(settings, sessions).resolve_toss(TOSS_LIVE_REFERENCE)

    _drive(scenario)
