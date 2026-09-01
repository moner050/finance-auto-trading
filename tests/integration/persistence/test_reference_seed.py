"""Preparing a database that Alembic has already migrated.

The seed existed and had no way to reach a database anybody uses - it was
applied by a paper harness and by tests, both of which build their own schema.
A production database therefore came up with every table and none of the rows,
and the first symptom was the accounts screen offering a broker to pick from
an empty table.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import integration_database_url
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.seed_reference import apply_reference_seed
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.accounts import Broker
from autotrader.persistence.mysql.models.core import CoreExchange, CoreMarket
from autotrader.persistence.mysql.models.david_v6 import DavidV6ManifestRow
from autotrader.persistence.mysql.models.strategy import StrategyVersion
from autotrader.persistence.mysql.repositories.core import CoreInstrumentRegistry
from autotrader.persistence.mysql.seeds.core import BINANCE_USDM_EXCHANGE_CODE, BROKERS
from autotrader.persistence.mysql.seeds.david_v6 import (
    DAVID_V6_VERSION_ID,
    SHADOW,
)
from autotrader.strategies.david_v6.manifest import (
    STRATEGY_VERSION,
    v6_configuration_hash,
)


def _drive(scenario: object) -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("MySQL is required for integration tests")

    async def run() -> None:
        settings = Settings(database_url=url)
        engine = create_engine(settings)
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await scenario(settings, sessions)  # type: ignore[operator]
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.integration
def test_the_seed_fills_a_migrated_database() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Broker)) == 0

        await apply_reference_seed(settings)

        async with sessions() as session:
            codes = set(
                (await session.scalars(select(Broker.code).order_by(Broker.code))).all()
            )
            assert codes == {code for _, code, _ in BROKERS}
            assert (
                await session.scalar(select(func.count()).select_from(CoreMarket)) > 0
            )
            assert (
                await session.scalar(select(func.count()).select_from(CoreExchange)) > 0
            )

    _drive(scenario)


@pytest.mark.integration
def test_applying_it_twice_changes_nothing() -> None:
    """Reference data is a statement about what has been implemented, not an
    event, so preparing an already prepared database is not an error."""

    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await apply_reference_seed(settings)
        await apply_reference_seed(settings)

        async with sessions() as session:
            assert await session.scalar(
                select(func.count()).select_from(Broker)
            ) == len(BROKERS)

    _drive(scenario)


@pytest.mark.integration
def test_the_broker_codes_are_the_ones_the_secret_store_uses() -> None:
    """A broker row whose code does not match the provider code makes an
    account that can never resolve its credentials."""
    from autotrader.apps.backoffice.provider_secrets import BINANCE, KIS, TOSS

    assert {code for _, code, _ in BROKERS} == {KIS, TOSS, BINANCE}


@pytest.mark.integration
def test_the_seed_registers_the_instrument_and_the_build() -> None:
    """`--check` named both and neither had an operational producer: the
    paper harness registered them and nothing else did, so a real database
    answered "no strategy manifest is registered" with no way to fix it."""

    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await apply_reference_seed(settings)

        async with sessions() as session:
            instrument = await CoreInstrumentRegistry(session).resolve(
                BINANCE_USDM_EXCHANGE_CODE, "BTCUSDT"
            )
            manifest = await session.scalar(select(DavidV6ManifestRow))

        assert instrument is not None
        assert manifest is not None
        assert manifest.strategy_version == STRATEGY_VERSION
        assert manifest.configuration_hash == v6_configuration_hash()

    _drive(scenario)


@pytest.mark.integration
def test_registering_the_same_build_twice_writes_one_manifest() -> None:
    """A build is a build. A second row for the same source and configuration
    would split the decisions taken under it across two ids."""

    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await apply_reference_seed(settings)
        await apply_reference_seed(settings)

        async with sessions() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(DavidV6ManifestRow)
                )
                == 1
            )

    _drive(scenario)


@pytest.mark.integration
def test_the_registered_version_starts_in_shadow() -> None:
    """Promotion to LIVE is section 11.8's job, behind two Shadow and two
    Paper sessions. A version registered as approved would skip all of it."""

    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await apply_reference_seed(settings)

        async with sessions() as session:
            version = await session.scalar(
                select(StrategyVersion).where(StrategyVersion.id == DAVID_V6_VERSION_ID)
            )

        assert version is not None
        assert version.status == SHADOW
        assert version.research_only is False

    _drive(scenario)
