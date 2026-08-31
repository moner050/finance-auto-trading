from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from unit.apps.trader.test_tick import _manifest, _risk_context

from autotrader.apps.trader.market_data import (
    HLIT_TIMEFRAME,
    BinanceContextSource,
    BinanceExecutionBars,
    BinanceLoopInputs,
)
from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import OrderStyle, Side
from autotrader.integrations.brokers.internal_paper import PaperOrderCommand
from autotrader.risk.v6 import V6RiskContext
from autotrader.shared.ids import new_uuid7
from autotrader.strategies.david_v6.costs import FeeSchedule
from autotrader.strategies.david_v6.models import V6Market
from autotrader.strategies.david_v6.order_flow import OrderFlowThresholds, TradePrint
from autotrader.strategies.david_v6.regime import PessimismInputs
from autotrader.strategies.david_v6.sessions import ExchangeCalendar, SessionKind

NOW = datetime(2026, 8, 25, 13, 30, tzinfo=UTC)


def _bar(at: datetime, close: str = "100") -> CompletedOhlcvBar:
    price = Decimal(close)
    return CompletedOhlcvBar(
        timestamp=at,
        open=price,
        high=price + Decimal(1),
        low=price - Decimal(1),
        close=price,
        volume=Decimal(10),
    )


class _Bars:
    def __init__(self, bars: tuple[CompletedOhlcvBar, ...]) -> None:
        self.bars = bars
        self.bar_calls = 0
        self.trade_calls = 0

    async def completed_bars(
        self, timeframe: timedelta, end_at: datetime
    ) -> tuple[CompletedOhlcvBar, ...]:
        del end_at
        self.bar_calls += 1
        if timeframe != HLIT_TIMEFRAME:
            return ()
        return self.bars

    async def trade_prints(
        self, start_at: datetime, end_at: datetime
    ) -> tuple[TradePrint, ...]:
        del start_at, end_at
        self.trade_calls += 1
        return ()


class _Risk:
    def __init__(self, context: V6RiskContext | None) -> None:
        self._context = context
        self.calls = 0

    def build(
        self, *, bars: tuple[CompletedOhlcvBar, ...], now: datetime
    ) -> V6RiskContext | None:
        del bars, now
        self.calls += 1
        return self._context


def _calendar() -> ExchangeCalendar:
    return ExchangeCalendar(
        session_date=NOW.date(),
        kind=SessionKind.BINANCE_USDM,
        source_timezone="UTC",
        is_trading_day=True,
        session_open_at=NOW - timedelta(hours=13),
        session_close_at=NOW + timedelta(hours=10),
        close_auction_at=None,
        pre_open_at=None,
        captured_at=NOW - timedelta(hours=14),
        valid_until=NOW + timedelta(hours=11),
    )


def _inputs() -> BinanceLoopInputs:
    return BinanceLoopInputs(
        instrument_id=new_uuid7(),
        manifest=_manifest(),
        calendar=_calendar(),
        order_flow_thresholds=OrderFlowThresholds(
            tick_size=Decimal("0.1"),
            delta_p90_notional=Decimal("500"),
            atr_30s=Decimal("1"),
            ceros_near_zero_notional=Decimal("10"),
            ceros_large_notional=Decimal("100"),
        ),
        fee_schedule=FeeSchedule(
            entry_fee_per_unit=Decimal("0.02"),
            exit_taker_fee_per_unit=Decimal("0.03"),
        ),
        tick_size=Decimal("0.1"),
        spread=Decimal("0.1"),
        stop_slippage_q95=Decimal("0.05"),
        quantity=Decimal(1),
        atr_ratio=Decimal("0.5"),
        range_efficiency=Decimal("0.5"),
        pessimism=PessimismInputs(
            completed_date=NOW.date(),
            volatility_percentile=Decimal("0.5"),
            put_call_percentile=Decimal("0.5"),
            breadth_percentile=Decimal("0.5"),
        ),
        benchmark_returns=tuple(Decimal("0.001") for _ in range(210)),
    )


def _source(bars: _Bars, risk: _Risk) -> BinanceContextSource:
    return BinanceContextSource(market_data=bars, inputs=_inputs(), risk=risk)


@pytest.mark.asyncio
async def test_a_closed_bar_produces_one_evaluation() -> None:
    bars = _Bars((_bar(NOW - timedelta(minutes=10)), _bar(NOW - timedelta(minutes=5))))
    source = _source(bars, _Risk(_risk_context()))

    context = await source.context_for(NOW)

    assert context is not None
    assert context.inputs.market is V6Market.BINANCE_USDM
    assert source.watermark == NOW - timedelta(minutes=5)


@pytest.mark.asyncio
async def test_the_same_bar_is_never_evaluated_twice() -> None:
    bars = _Bars((_bar(NOW - timedelta(minutes=5)),))
    source = _source(bars, _Risk(_risk_context()))

    assert await source.context_for(NOW) is not None
    # Evidence that has not changed must not record a second decision.
    assert await source.context_for(NOW + timedelta(minutes=1)) is None


@pytest.mark.asyncio
async def test_a_newer_bar_resumes_evaluation() -> None:
    bars = _Bars((_bar(NOW - timedelta(minutes=5)),))
    source = _source(bars, _Risk(_risk_context()))
    assert await source.context_for(NOW) is not None

    bars.bars = (*bars.bars, _bar(NOW))
    assert await source.context_for(NOW + timedelta(minutes=5)) is not None
    assert source.watermark == NOW


@pytest.mark.asyncio
async def test_no_bars_yields_no_evaluation() -> None:
    source = _source(_Bars(()), _Risk(_risk_context()))

    assert await source.context_for(NOW) is None
    assert source.watermark is None


@pytest.mark.asyncio
async def test_an_unpriceable_account_does_not_advance_the_watermark() -> None:
    bars = _Bars((_bar(NOW - timedelta(minutes=5)),))
    risk = _Risk(None)
    source = _source(bars, risk)

    assert await source.context_for(NOW) is None
    # The bar was never evaluated, so a later pass must still see it.
    assert source.watermark is None
    assert await source.context_for(NOW + timedelta(minutes=1)) is None
    assert risk.calls == 2


def _command(signal_at: datetime) -> PaperOrderCommand:
    return PaperOrderCommand(
        id=new_uuid7(),
        order_id=new_uuid7(),
        account_alias="internal-binance-usdm-paper",
        market=V6Market.BINANCE_USDM,
        side=Side.BUY,
        order_style=OrderStyle.LIMIT,
        quantity=Decimal(1),
        limit_price=Decimal(100),
        signal_at=signal_at,
        timeframe=HLIT_TIMEFRAME,
        fee_per_unit=Decimal("0.01"),
        slippage_per_unit=Decimal("0.01"),
    )


@pytest.mark.asyncio
async def test_the_fill_bar_is_the_one_exactly_after_the_signal() -> None:
    signal_at = NOW - timedelta(minutes=5)
    bars = _Bars((_bar(signal_at), _bar(NOW, "101")))

    execution_bar = await BinanceExecutionBars(bars).bar_at(
        _command(signal_at), now=NOW
    )

    assert execution_bar is not None
    assert execution_bar.bar.timestamp == NOW
    assert execution_bar.bar.close == Decimal("101")
    assert len(execution_bar.source_digest) == 32


@pytest.mark.asyncio
async def test_a_bar_that_has_not_closed_is_not_a_missing_bar() -> None:
    signal_at = NOW
    bars = _Bars((_bar(NOW - timedelta(minutes=5)), _bar(NOW)))

    # The fill bar would be NOW + 5m, which has not closed.
    assert await BinanceExecutionBars(bars).bar_at(_command(signal_at), now=NOW) is None


def _resting_stop(signal_at: datetime, trigger: str) -> PaperOrderCommand:
    """A protective stop: market style, resolved by whichever bar reaches it."""
    return PaperOrderCommand(
        id=new_uuid7(),
        order_id=new_uuid7(),
        account_alias="internal-binance-usdm-paper",
        market=V6Market.BINANCE_USDM,
        side=Side.SELL,
        order_style=OrderStyle.MARKET,
        quantity=Decimal(1),
        limit_price=None,
        signal_at=signal_at,
        timeframe=HLIT_TIMEFRAME,
        fee_per_unit=Decimal("0.01"),
        slippage_per_unit=Decimal("0.01"),
        trigger_price=Decimal(trigger),
    )


@pytest.mark.asyncio
async def test_a_stop_rests_while_no_bar_reaches_it() -> None:
    signal_at = NOW - timedelta(minutes=10)
    # Both later bars trade between 99 and 101, never touching 90.
    bars = _Bars((_bar(signal_at), _bar(NOW - timedelta(minutes=5)), _bar(NOW)))

    resting = await BinanceExecutionBars(bars).bar_at(
        _resting_stop(signal_at, "90"), now=NOW
    )

    # Nothing reached the stop, which is not the same as a missing bar.
    assert resting is None


@pytest.mark.asyncio
async def test_a_stop_is_resolved_by_the_first_bar_that_reaches_it() -> None:
    signal_at = NOW - timedelta(minutes=15)
    breached = _bar(NOW - timedelta(minutes=5), "95")
    bars = _Bars(
        (
            _bar(signal_at),
            _bar(NOW - timedelta(minutes=10)),
            breached,
            _bar(NOW, "94"),
        )
    )

    resolved = await BinanceExecutionBars(bars).bar_at(
        # The stop sits at 94.5, which only the third bar's low of 94 reaches.
        _resting_stop(signal_at, "94.5"),
        now=NOW,
    )

    assert resolved is not None
    # The first bar to reach it, not the latest bar available.
    assert resolved.bar.timestamp == breached.timestamp


@pytest.mark.asyncio
async def test_a_stop_is_never_resolved_by_a_bar_from_before_it_existed() -> None:
    signal_at = NOW - timedelta(minutes=5)
    # An earlier bar reaches the stop price, but the order did not exist yet.
    bars = _Bars((_bar(NOW - timedelta(minutes=10), "90"), _bar(NOW)))

    assert (
        await BinanceExecutionBars(bars).bar_at(_resting_stop(signal_at, "90"), now=NOW)
        is None
    )
