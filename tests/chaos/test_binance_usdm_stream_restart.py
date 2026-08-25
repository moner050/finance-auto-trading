from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from autotrader.integrations.market_data.binance_usdm import (
    BinanceUsdmMarketData,
    BinanceUsdmMarketDataError,
)
from tests.unit.integrations.market_data.test_binance_usdm import (
    START,
    Rest,
    Store,
    event,
    market,
    rest_trade,
)


@pytest.mark.asyncio
async def test_restart_loads_checkpoint_and_recovers_only_the_exact_gap() -> None:
    store = Store()
    first = market(store=store)
    await first.ingest_agg_trade(event(100, START + timedelta(seconds=1)))

    rest = Rest(
        aggregate_trade_rows={
            101: rest_trade(101, START + timedelta(seconds=2)),
            102: rest_trade(102, START + timedelta(seconds=3)),
        }
    )
    restarted = market(rest, store)
    await restarted.ingest_agg_trade(event(103, START + timedelta(seconds=4)))

    assert rest.aggregate_calls == [("BTCUSDT", 101, 2)]
    assert store.checkpoint is not None
    assert store.checkpoint.last_aggregate_trade_id == 103
    assert [
        trade.provider_trade_id
        for trade in await restarted.trade_prints(START, START + timedelta(seconds=5))
    ] == ["100", "101", "102", "103"]


@pytest.mark.asyncio
async def test_large_restart_gap_is_recovered_in_bounded_provider_pages() -> None:
    store = Store()
    first = market(store=store)
    await first.ingest_agg_trade(event(1, START + timedelta(milliseconds=1)))
    rows = {
        value: rest_trade(value, START + timedelta(milliseconds=value))
        for value in range(2, 1003)
    }
    rest = Rest(aggregate_trade_rows=rows)
    restarted = market(rest, store)

    await restarted.ingest_agg_trade(event(1003, START + timedelta(milliseconds=1003)))

    assert rest.aggregate_calls == [
        ("BTCUSDT", 2, 1000),
        ("BTCUSDT", 1002, 1),
    ]
    assert store.checkpoint is not None
    assert store.checkpoint.last_aggregate_trade_id == 1003


@pytest.mark.asyncio
async def test_restart_does_not_advance_checkpoint_across_an_unfilled_gap() -> None:
    store = Store()
    first = market(store=store)
    await first.ingest_agg_trade(event(10, START + timedelta(seconds=1)))
    rest = Rest(
        aggregate_trade_rows={
            11: rest_trade(11, START + timedelta(seconds=2)),
            13: rest_trade(13, START + timedelta(seconds=3)),
        }
    )
    restarted = market(rest, store)

    with pytest.raises(BinanceUsdmMarketDataError, match="gap"):
        await restarted.ingest_agg_trade(event(14, START + timedelta(seconds=4)))

    assert store.checkpoint is not None
    assert store.checkpoint.last_aggregate_trade_id == 10


@pytest.mark.asyncio
async def test_completed_30s_bar_can_be_proven_after_restart() -> None:
    store = Store()
    first = market(store=store)
    await first.ingest_agg_trade(event(1, START + timedelta(seconds=1)))
    restarted: BinanceUsdmMarketData = market(store=store)
    await restarted.ingest_agg_trade(event(2, START + timedelta(seconds=30)))

    bars = await restarted.completed_bars(
        timedelta(seconds=30),
        START + timedelta(seconds=30),
    )

    assert len(bars) == 1
    assert bars[0].timestamp == datetime(2026, 8, 24, 0, 0, 30, tzinfo=UTC)
