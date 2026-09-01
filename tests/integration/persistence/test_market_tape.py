"""The market store, against a real MySQL.

`BinanceUsdmMarketData` deduplicates against this and resumes from its
checkpoint. It was a Protocol nobody implemented, which meant the loop could
not read a trade window at all - and the order-flow observations, the
thirty-second ATR and the extreme-delta threshold are all taken over the tape.

The cases that matter are the ones a restart produces: the same window fetched
twice, and a checkpoint that must not walk backwards.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from conftest import integration_database_url
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.integrations.market_data.binance_usdm import (
    BinanceUsdmMarketCheckpoint,
)
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.market_tape import (
    MarketTapeCheckpointRow,
    MarketTradePrintRow,
)
from autotrader.persistence.mysql.repositories.market_tape import MySqlMarketTape
from autotrader.strategies.david_v6.order_flow import TradePrint

SYMBOL = "BTCUSDT"
START = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _drive(scenario: object) -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("MySQL is required for integration tests")

    async def run() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await _clear(sessions)
            await scenario(sessions)  # type: ignore[operator]
        finally:
            await _clear(sessions)
            await engine.dispose()

    asyncio.run(run())


async def _clear(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        await session.execute(delete(MarketTradePrintRow))
        await session.execute(delete(MarketTapeCheckpointRow))
        await session.commit()


def _trade(index: int, *, buyer_maker: bool | None = False) -> TradePrint:
    return TradePrint(
        provider_trade_id=f"agg-{index}",
        occurred_at=START + timedelta(seconds=index),
        price=Decimal("70000") + Decimal(index),
        quantity=Decimal("0.01"),
        buyer_maker=buyer_maker,
    )


def _checkpoint(last_id: int) -> BinanceUsdmMarketCheckpoint:
    return BinanceUsdmMarketCheckpoint(
        symbol=SYMBOL,
        last_aggregate_trade_id=last_id,
        last_trade_at=START + timedelta(seconds=last_id),
    )


@pytest.mark.integration
def test_a_window_survives_a_restart() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        tape = MySqlMarketTape(sessions)
        await tape.persist(SYMBOL, tuple(_trade(i) for i in range(5)), _checkpoint(4))

        # A second instance, reading what the first stored.
        loaded = await MySqlMarketTape(sessions).load_trades(
            SYMBOL, START, START + timedelta(seconds=10)
        )
        checkpoint = await MySqlMarketTape(sessions).load_checkpoint(SYMBOL)

        assert [trade.provider_trade_id for trade in loaded] == [
            f"agg-{index}" for index in range(5)
        ]
        assert checkpoint is not None
        assert checkpoint.last_aggregate_trade_id == 4

    _drive(scenario)


@pytest.mark.integration
def test_the_same_window_fetched_twice_stores_it_once() -> None:
    """Re-fetching evidence after a restart is normal, and must not be an
    error or a duplicate."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        tape = MySqlMarketTape(sessions)
        trades = tuple(_trade(i) for i in range(5))
        await tape.persist(SYMBOL, trades, _checkpoint(4))
        await tape.persist(SYMBOL, trades, _checkpoint(4))

        async with sessions() as session:
            count = await session.scalar(
                select(func.count()).select_from(MarketTradePrintRow)
            )
        assert count == 5

    _drive(scenario)


@pytest.mark.integration
def test_the_checkpoint_never_walks_backwards() -> None:
    """Two instances briefly overlapping would otherwise send the next fetch
    back over a window that was already stored."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        tape = MySqlMarketTape(sessions)
        await tape.persist(SYMBOL, (_trade(9),), _checkpoint(9))
        await tape.persist(SYMBOL, (_trade(3),), _checkpoint(3))

        checkpoint = await tape.load_checkpoint(SYMBOL)

        assert checkpoint is not None
        assert checkpoint.last_aggregate_trade_id == 9

    _drive(scenario)


@pytest.mark.integration
def test_a_trade_is_found_by_the_venue_id_it_was_stored_under() -> None:
    """That id is what deduplication is done on."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        tape = MySqlMarketTape(sessions)
        await tape.persist(SYMBOL, (_trade(7),), _checkpoint(7))

        found = await tape.find_trade(SYMBOL, "agg-7")
        missing = await tape.find_trade(SYMBOL, "agg-8")

        assert found is not None
        assert found.price == Decimal("70007")
        assert missing is None

    _drive(scenario)


@pytest.mark.integration
def test_the_window_is_half_open() -> None:
    """The loop reads back-to-back windows, and a closed upper bound would
    count the boundary trade in both."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        tape = MySqlMarketTape(sessions)
        await tape.persist(SYMBOL, tuple(_trade(i) for i in range(4)), _checkpoint(3))

        first = await tape.load_trades(SYMBOL, START, START + timedelta(seconds=2))
        second = await tape.load_trades(
            SYMBOL, START + timedelta(seconds=2), START + timedelta(seconds=4)
        )

        assert [trade.provider_trade_id for trade in first] == ["agg-0", "agg-1"]
        assert [trade.provider_trade_id for trade in second] == ["agg-2", "agg-3"]

    _drive(scenario)


@pytest.mark.integration
def test_an_unknown_aggressor_is_stored_as_unknown() -> None:
    """The order flow counts one rather than guessing a side."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        tape = MySqlMarketTape(sessions)
        await tape.persist(SYMBOL, (_trade(1, buyer_maker=None),), _checkpoint(1))

        loaded = await tape.load_trades(SYMBOL, START, START + timedelta(seconds=5))

        assert loaded[0].buyer_maker is None

    _drive(scenario)
