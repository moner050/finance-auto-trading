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
from autotrader.persistence.mysql.seeds.core import BROKERS


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
