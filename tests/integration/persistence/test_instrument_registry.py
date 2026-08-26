"""Instrument registration against a real MySQL.

Every v6 decision names a canonical instrument, and before this path existed
a caller could only invent an id that no table had heard of. What the registry
has to guarantee is that identity survives: the same listing is always the
same id, and a delisting never takes the id out from under the decisions that
already reference it.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from conftest import integration_database_url
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.core import CoreInstrument
from autotrader.persistence.mysql.repositories.core import (
    CoreInstrumentRegistry,
    InstrumentListing,
    InstrumentNotRegisteredError,
    UnknownExchangeError,
)
from autotrader.persistence.mysql.seeds.core import (
    BINANCE_USDM_EXCHANGE_CODE,
    seed_core_reference_session,
)

ROOT = Path(__file__).resolve().parents[3]

BTCUSDT = InstrumentListing(
    exchange_code=BINANCE_USDM_EXCHANGE_CODE,
    code="BTCUSDT",
    name="BTCUSDT Perpetual",
    instrument_type="PERPETUAL",
)


def _run(scenario: object) -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")
    command.upgrade(Config(ROOT / "alembic.ini"), "head")

    async def drive() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            async with sessions() as session:
                await seed_core_reference_session(session)
                await session.commit()
            async with sessions() as session:
                await scenario(session)  # type: ignore[operator]
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(drive())


@pytest.mark.integration
def test_registering_the_same_listing_twice_keeps_one_identity() -> None:
    async def scenario(session: AsyncSession) -> None:
        registry = CoreInstrumentRegistry(session)

        first = await registry.register(BTCUSDT)
        second = await registry.register(BTCUSDT)

        assert first == second
        assert await registry.resolve(BTCUSDT.exchange_code, BTCUSDT.code) == first
        rows = (await session.scalars(select(CoreInstrument))).all()
        assert [row.id for row in rows] == [first]

    _run(scenario)


@pytest.mark.integration
def test_a_renamed_listing_keeps_the_id_the_decisions_point_at() -> None:
    async def scenario(session: AsyncSession) -> None:
        registry = CoreInstrumentRegistry(session)
        original = await registry.register(BTCUSDT)

        renamed = await registry.register(
            replace(BTCUSDT, name="Bitcoin USDT Perpetual")
        )

        assert renamed == original
        stored = await session.get(CoreInstrument, original)
        assert stored is not None
        assert stored.name == "Bitcoin USDT Perpetual"

    _run(scenario)


@pytest.mark.integration
def test_an_unregistered_instrument_is_refused_rather_than_invented() -> None:
    async def scenario(session: AsyncSession) -> None:
        registry = CoreInstrumentRegistry(session)

        with pytest.raises(InstrumentNotRegisteredError):
            await registry.resolve(BINANCE_USDM_EXCHANGE_CODE, "ETHUSDT")

    _run(scenario)


@pytest.mark.integration
def test_a_code_reused_for_a_different_contract_is_refused() -> None:
    async def scenario(session: AsyncSession) -> None:
        registry = CoreInstrumentRegistry(session)
        await registry.register(BTCUSDT)

        # Reusing the id would hand a spot listing every decision recorded
        # against the perpetual.
        with pytest.raises(ValueError, match="already registered as PERPETUAL"):
            await registry.register(replace(BTCUSDT, instrument_type="SPOT"))

    _run(scenario)


@pytest.mark.integration
def test_a_delisted_instrument_keeps_its_row_and_can_come_back() -> None:
    async def scenario(session: AsyncSession) -> None:
        registry = CoreInstrumentRegistry(session)
        original = await registry.register(BTCUSDT)

        delisted = await registry.delist(BTCUSDT.exchange_code, BTCUSDT.code)

        assert delisted == original
        stored = await session.get(CoreInstrument, original)
        assert stored is not None
        assert stored.status == "INACTIVE"
        with pytest.raises(InstrumentNotRegisteredError):
            await registry.resolve(BTCUSDT.exchange_code, BTCUSDT.code)

        # A relisting is the same instrument coming back, not a new one.
        assert await registry.register(BTCUSDT) == original

    _run(scenario)


@pytest.mark.integration
def test_a_listing_under_an_unknown_exchange_is_refused() -> None:
    async def scenario(session: AsyncSession) -> None:
        registry = CoreInstrumentRegistry(session)

        with pytest.raises(UnknownExchangeError):
            await registry.register(replace(BTCUSDT, exchange_code="BINANCE_TYPO"))

    _run(scenario)
