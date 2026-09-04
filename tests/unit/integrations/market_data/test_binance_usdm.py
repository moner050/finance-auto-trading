from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from autotrader.integrations.market_data.binance_usdm import (
    BinanceUsdmMarketCheckpoint,
    BinanceUsdmMarketData,
    BinanceUsdmMarketDataError,
)
from autotrader.strategies.david_v6.order_flow import TradePrint

START = datetime(2026, 8, 24, tzinfo=UTC)


def _ms(value: datetime) -> int:
    return (value - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)


def _kline(start: datetime, duration: timedelta, close: str = "101") -> list[object]:
    opened = _ms(start)
    completed = _ms(start + duration)
    return [
        opened,
        "100",
        "102",
        "99",
        close,
        "3.5",
        completed - 1,
        "0",
        3,
        "0",
        "0",
        "0",
    ]


def event(
    trade_id: int,
    at: datetime,
    *,
    price: str = "100",
    quantity: str = "1",
    buyer_maker: bool = False,
    symbol: str = "BTCUSDT",
) -> dict[str, object]:
    return {
        "e": "aggTrade",
        "s": symbol,
        "a": trade_id,
        "p": price,
        "q": quantity,
        "f": trade_id,
        "l": trade_id,
        "T": _ms(at),
        "m": buyer_maker,
    }


def rest_trade(
    trade_id: int,
    at: datetime,
    *,
    price: str = "100",
) -> dict[str, object]:
    value = event(trade_id, at, price=price)
    value.pop("e")
    value.pop("s")
    return value


@dataclass
class Rest:
    klines_by_interval: dict[str, tuple[object, ...]] = field(
        default_factory=dict[str, tuple[object, ...]]
    )
    aggregate_trade_rows: dict[int, dict[str, object]] = field(
        default_factory=dict[int, dict[str, object]]
    )
    kline_calls: list[tuple[str, str, int, int]] = field(default_factory=lambda: [])
    aggregate_calls: list[tuple[str, int, int]] = field(default_factory=lambda: [])

    async def klines(
        self,
        *,
        symbol: str,
        interval: str,
        end_time_ms: int,
        limit: int,
    ) -> tuple[object, ...]:
        self.kline_calls.append((symbol, interval, end_time_ms, limit))
        return self.klines_by_interval.get(interval, ())

    async def aggregate_trades(
        self,
        *,
        symbol: str,
        from_id: int,
        limit: int,
    ) -> tuple[object, ...]:
        self.aggregate_calls.append((symbol, from_id, limit))
        return tuple(
            self.aggregate_trade_rows[value]
            for value in sorted(self.aggregate_trade_rows)
            if from_id <= value < from_id + limit
        )


@dataclass
class Store:
    checkpoint: BinanceUsdmMarketCheckpoint | None = None
    trades: dict[str, TradePrint] = field(default_factory=dict[str, TradePrint])
    batches: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])
    # The window each read asked the database for. What the aggregation
    # costs is decided here rather than in the bars that come back.
    loads: list[tuple[datetime, datetime]] = field(
        default_factory=list[tuple[datetime, datetime]]
    )
    symbol: str = "BTCUSDT"

    async def load_checkpoint(self, symbol: str) -> BinanceUsdmMarketCheckpoint | None:
        assert symbol == self.symbol
        return self.checkpoint

    async def find_trade(
        self,
        symbol: str,
        provider_trade_id: str,
    ) -> TradePrint | None:
        assert symbol == self.symbol
        return self.trades.get(provider_trade_id)

    async def persist(
        self,
        symbol: str,
        trades: tuple[TradePrint, ...],
        checkpoint: BinanceUsdmMarketCheckpoint,
    ) -> None:
        assert symbol == self.symbol
        for trade in trades:
            previous = self.trades.get(trade.provider_trade_id)
            if previous is not None and previous != trade:
                raise ValueError("trade correction")
            self.trades[trade.provider_trade_id] = trade
        self.checkpoint = checkpoint
        self.batches.append(tuple(trade.provider_trade_id for trade in trades))

    async def load_trades(
        self,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[TradePrint, ...]:
        assert symbol == self.symbol
        self.loads.append((start_at, end_at))
        return tuple(
            trade
            for trade in sorted(
                self.trades.values(),
                key=lambda item: (item.occurred_at, item.provider_trade_id),
            )
            if start_at <= trade.occurred_at < end_at
        )


def market(
    rest: Rest | None = None,
    store: Store | None = None,
    symbol: str = "BTCUSDT",
) -> BinanceUsdmMarketData:
    return BinanceUsdmMarketData(
        rest=Rest() if rest is None else rest,
        store=Store(symbol=symbol) if store is None else store,
        symbol=symbol,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeframe", "interval"),
    (
        (timedelta(days=1), "1d"),
        (timedelta(hours=1), "1h"),
        (timedelta(minutes=15), "15m"),
        (timedelta(minutes=5), "5m"),
        (timedelta(minutes=1), "1m"),
    ),
)
async def test_returns_only_completed_rest_klines_at_the_exact_boundary(
    timeframe: timedelta,
    interval: str,
) -> None:
    rest = Rest(
        klines_by_interval={
            interval: (
                _kline(START, timeframe),
                _kline(START + timeframe, timeframe, close="102"),
            )
        }
    )
    subject = market(rest)

    bars = await subject.completed_bars(timeframe, START + timeframe)

    assert len(bars) == 1
    assert bars[0].timestamp == START + timeframe
    assert bars[0].open == Decimal("100")
    assert bars[0].high == Decimal("102")
    assert bars[0].low == Decimal("99")
    assert bars[0].close == Decimal("101")
    assert bars[0].volume == Decimal("3.5")
    assert rest.kline_calls == [("BTCUSDT", interval, _ms(START + timeframe), 1500)]


@pytest.mark.asyncio
async def test_aggregates_completed_30s_bars_and_separate_5s_telemetry() -> None:
    store = Store()
    subject = market(store=store)
    await subject.ingest_agg_trade(
        event(1, START + timedelta(seconds=1), price="100", quantity="1")
    )
    await subject.ingest_agg_trade(
        event(2, START + timedelta(seconds=4), price="102", quantity="2")
    )
    await subject.ingest_agg_trade(
        event(3, START + timedelta(seconds=29), price="99", quantity="3")
    )
    await subject.ingest_agg_trade(
        event(4, START + timedelta(seconds=30), price="101", quantity="1")
    )

    bars = await subject.completed_bars(
        timedelta(seconds=30),
        START + timedelta(seconds=30),
        history=timedelta(hours=2),
    )
    telemetry = await subject.telemetry_bars(START + timedelta(seconds=30))

    assert len(bars) == 1
    assert bars[0].timestamp == START + timedelta(seconds=30)
    assert bars[0].open == Decimal("100")
    assert bars[0].high == Decimal("102")
    assert bars[0].low == Decimal("99")
    assert bars[0].close == Decimal("99")
    assert bars[0].volume == Decimal("6")
    assert [bar.timestamp for bar in telemetry] == [
        START + timedelta(seconds=5),
        START + timedelta(seconds=30),
    ]
    with pytest.raises(ValueError, match="timeframe"):
        await subject.completed_bars(
            timedelta(seconds=5), START + timedelta(seconds=30)
        )


@pytest.mark.asyncio
async def test_deduplicates_and_ignores_old_out_of_order_stream_events() -> None:
    store = Store()
    subject = market(store=store)
    value = event(10, START + timedelta(seconds=10))

    await subject.ingest_agg_trade(value)
    await subject.ingest_agg_trade(value)
    await subject.ingest_agg_trade(event(9, START + timedelta(seconds=9)))

    assert store.batches == [("10",)]
    with pytest.raises(BinanceUsdmMarketDataError, match="correction"):
        await subject.ingest_agg_trade(
            event(10, START + timedelta(seconds=10), price="101")
        )


@pytest.mark.asyncio
async def test_recovers_a_websocket_gap_from_rest_before_persisting_checkpoint() -> (
    None
):
    rest = Rest(
        aggregate_trade_rows={
            101: rest_trade(101, START + timedelta(seconds=2)),
            102: rest_trade(102, START + timedelta(seconds=3)),
        }
    )
    store = Store()
    subject = market(rest, store)
    await subject.ingest_agg_trade(event(100, START + timedelta(seconds=1)))

    await subject.ingest_agg_trade(event(103, START + timedelta(seconds=4)))

    assert rest.aggregate_calls == [("BTCUSDT", 101, 2)]
    assert store.batches == [("100",), ("101", "102", "103")]
    assert store.checkpoint is not None
    assert store.checkpoint.last_aggregate_trade_id == 103
    trades = await subject.trade_prints(START, START + timedelta(seconds=5))
    assert [trade.provider_trade_id for trade in trades] == ["100", "101", "102", "103"]


@pytest.mark.asyncio
async def test_missing_rest_gap_keeps_the_previous_checkpoint() -> None:
    rest = Rest(
        aggregate_trade_rows={
            101: rest_trade(101, START + timedelta(seconds=2)),
        }
    )
    store = Store()
    subject = market(rest, store)
    await subject.ingest_agg_trade(event(100, START + timedelta(seconds=1)))

    with pytest.raises(BinanceUsdmMarketDataError, match="gap"):
        await subject.ingest_agg_trade(event(103, START + timedelta(seconds=4)))

    assert store.checkpoint is not None
    assert store.checkpoint.last_aggregate_trade_id == 100
    assert store.batches == [("100",)]


@pytest.mark.asyncio
async def test_rejects_forming_aggregate_bad_symbol_and_kline_correction() -> None:
    store = Store()
    subject = market(store=store)
    await subject.ingest_agg_trade(event(1, START + timedelta(seconds=1)))

    assert (
        await subject.completed_bars(
            timedelta(seconds=30),
            START + timedelta(seconds=30),
            history=timedelta(hours=2),
        )
        == ()
    )
    wrong = event(2, START + timedelta(seconds=2))
    wrong["s"] = "ETHUSDT"
    with pytest.raises(BinanceUsdmMarketDataError, match="BTCUSDT"):
        await subject.ingest_agg_trade(wrong)

    duration = timedelta(minutes=1)
    first = _kline(START, duration)
    corrected = _kline(START, duration, close="100.5")
    rest = Rest(klines_by_interval={"1m": (first, corrected)})
    with pytest.raises(BinanceUsdmMarketDataError, match="correction"):
        await market(rest).completed_bars(duration, START + duration)


@pytest.mark.asyncio
async def test_trade_print_range_is_start_inclusive_end_exclusive() -> None:
    subject = market()
    await subject.ingest_agg_trade(event(1, START))
    await subject.ingest_agg_trade(event(2, START + timedelta(seconds=1)))
    await subject.ingest_agg_trade(event(3, START + timedelta(seconds=2)))

    trades = await subject.trade_prints(
        START + timedelta(seconds=1),
        START + timedelta(seconds=2),
    )

    assert [trade.provider_trade_id for trade in trades] == ["2"]


@pytest.mark.asyncio
async def test_a_reader_holds_one_symbol_and_refuses_the_others() -> None:
    """The pin is per instance, and a frame for another instrument is refused.

    It used to be a module constant, which is why only one instrument could
    ever be collected. Moving it onto the reader is what allows a second
    tape; what it must not allow is one reader accepting another's frames,
    because that files ETHUSDT prints under BTCUSDT where the symbol column
    then says the wrong thing and nothing downstream can tell.
    """
    store = Store(symbol="ETHUSDT")
    ethereum = market(store=store, symbol="ETHUSDT")

    await ethereum.ingest_agg_trade(event(1, START, symbol="ETHUSDT"))

    assert ethereum.symbol == "ETHUSDT"
    assert store.checkpoint is not None
    assert store.checkpoint.symbol == "ETHUSDT"
    with pytest.raises(BinanceUsdmMarketDataError, match="requires ETHUSDT"):
        await ethereum.ingest_agg_trade(event(2, START, symbol="BTCUSDT"))


def test_a_checkpoint_still_needs_a_symbol() -> None:
    """Dropping the BTCUSDT-only rule is not dropping the check."""
    with pytest.raises(ValueError, match="needs a symbol"):
        BinanceUsdmMarketCheckpoint(
            symbol="ethusdt",
            last_aggregate_trade_id=1,
            last_trade_at=START,
        )


@pytest.mark.asyncio
async def test_thirty_second_bars_read_only_the_history_they_were_given() -> None:
    """The depth decides the cost, so the caller has to name it.

    It used to be refused - `history` was a five-minute idea and thirty
    seconds took a module constant of one day instead. On BTCUSDT that is
    1.8 million prints and 145 seconds of MySQL for a search that reads two
    hours; the plan's section 33.13 measured both. The number is the same
    either way, and the difference is whether the call site can see it.
    """
    store = Store()
    subject = market(store=store)
    end = START + timedelta(hours=3)
    # One print inside the last hour and one well behind it, plus a third
    # that only exists to carry the watermark past the second: a bucket whose
    # close has not been reached is still forming and is rightly dropped, and
    # without this the shallow read would be empty for that reason instead of
    # the one under test. Its own bucket is the one left forming.
    await subject.ingest_agg_trade(
        event(1, end - timedelta(hours=2), price="100", quantity="1")
    )
    await subject.ingest_agg_trade(
        event(2, end - timedelta(minutes=30), price="105", quantity="2")
    )
    await subject.ingest_agg_trade(
        event(3, end - timedelta(seconds=1), price="999", quantity="1")
    )

    shallow = await subject.completed_bars(
        timedelta(seconds=30), end, history=timedelta(hours=1)
    )
    deep = await subject.completed_bars(
        timedelta(seconds=30), end, history=timedelta(hours=3)
    )

    assert [bar.close for bar in shallow] == [Decimal("105")]
    assert [bar.close for bar in deep] == [Decimal("100"), Decimal("105")]
    assert store.loads[-2:] == [
        (end - timedelta(hours=1), end + timedelta(milliseconds=1)),
        (end - timedelta(hours=3), end + timedelta(milliseconds=1)),
    ]


@pytest.mark.asyncio
async def test_thirty_second_bars_refuse_an_unstated_or_empty_history() -> None:
    subject = market()

    with pytest.raises(ValueError, match="stated history"):
        await subject.completed_bars(timedelta(seconds=30), START)
    with pytest.raises(ValueError, match="positive"):
        await subject.completed_bars(timedelta(seconds=30), START, history=timedelta(0))
