"""One evaluation per closed bar, and what happens when a pass cannot measure.

The watermark decides what goes on record. Advancing it on a pass that
produced nothing would skip that bar for good; not advancing it on a pass that
produced a decision would record the same bar twice, and the promotion
evidence counts decisions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid7

import pytest

from autotrader.apps.trader.market_data import BinanceLoopInputs
from autotrader.apps.trader.risk_context import AccountBudget, BinanceRiskContexts
from autotrader.apps.trader.shadow_source import ShadowContextSource
from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.integrations.market_data.binance_session import (
    binance_usdm_calendar,
    session_date_for,
)
from autotrader.persistence.mysql.repositories.david_v6_risk import approved_v6_policy
from autotrader.shared.ids import new_uuid7
from autotrader.strategies.david_v6.costs import FeeSchedule
from autotrader.strategies.david_v6.manifest import (
    V6_DESIGN_SHA256,
    V6_SOURCE_SHA256,
    V6Manifest,
    v6_configuration_hash,
)
from autotrader.strategies.david_v6.models import V6Market
from autotrader.strategies.david_v6.order_flow import OrderFlowThresholds, TradePrint
from autotrader.strategies.david_v6.regime import PessimismInputs

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _built() -> BinanceLoopInputs:
    """One pass's assembled inputs, valid enough for the assembly to accept."""
    return BinanceLoopInputs(
        instrument_id=uuid7(),
        manifest=V6Manifest(
            id=uuid7(),
            strategy_version_id=uuid7(),
            source_sha256=V6_SOURCE_SHA256,
            design_sha256=V6_DESIGN_SHA256,
            configuration_hash=v6_configuration_hash(),
            registered_at=NOW.replace(microsecond=0),
        ),
        calendar=binance_usdm_calendar(
            session_date=session_date_for(NOW), captured_at=NOW
        ),
        order_flow_thresholds=OrderFlowThresholds(
            tick_size=Decimal("0.1"), atr_30s=Decimal("1")
        ),
        fee_schedule=FeeSchedule(
            entry_fee_per_unit=Decimal("30"),
            exit_taker_fee_per_unit=Decimal("30"),
        ),
        tick_size=Decimal("0.1"),
        spread=Decimal("0.1"),
        stop_slippage_q95=Decimal("1"),
        quantity=Decimal("0.001"),
        pessimism=PessimismInputs(
            completed_date=NOW.date(),
            volatility_percentile=Decimal("0.5"),
            put_call_percentile=None,
            breadth_percentile=Decimal("0.5"),
        ),
        benchmark_returns=(Decimal("0.01"),) * 3,
    )


def _bars(count: int, *, end: datetime = NOW) -> tuple[CompletedOhlcvBar, ...]:
    return tuple(
        CompletedOhlcvBar(
            timestamp=end - timedelta(minutes=5 * (count - index)),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("10"),
        )
        for index in range(count)
    )


class _Market:
    def __init__(
        self,
        bars: tuple[CompletedOhlcvBar, ...],
        execution: tuple[CompletedOhlcvBar, ...] | None = None,
    ) -> None:
        self.bars = bars
        # Answering every timeframe with the same series hid the thirty-second
        # request entirely: the source could ask for it or not and the test
        # read the same. Thirty seconds gets its own answer, and `None` is a
        # venue that has no tape to build it from.
        self.execution = execution
        self.asked_history: list[timedelta | None] = []
        self.asked: list[timedelta] = []

    async def completed_bars(
        self,
        timeframe: timedelta,
        now: datetime,
        *,
        history: timedelta | None = None,
    ) -> tuple[CompletedOhlcvBar, ...]:
        del now
        self.asked_history.append(history)
        self.asked.append(timeframe)
        if timeframe == timedelta(seconds=30):
            return () if self.execution is None else self.execution
        return self.bars

    async def trade_prints(
        self, start: datetime, end: datetime
    ) -> tuple[TradePrint, ...]:
        del start, end
        return ()


def _risk_contexts() -> BinanceRiskContexts:
    """The real builder, against the approved policy for this market."""
    return BinanceRiskContexts(
        budget=AccountBudget(
            session_start_equity=Decimal("362"),
            current_equity=Decimal("362"),
            quantity_step=Decimal("0.001"),
            tick_size=Decimal("0.1"),
            spread=Decimal("0.1"),
            cost_per_unit=Decimal("30"),
            leverage=3,
        ),
        policy=approved_v6_policy(V6Market.BINANCE_USDM, policy_version_id=new_uuid7()),
    )


class _NoRisk:
    """A pass that cannot size an order."""

    def __init__(self) -> None:
        self.calls = 0

    def build(self, *, bars: object, now: datetime, side: object) -> None:
        del bars, now, side
        self.calls += 1
        return None


class _Inputs:
    def __init__(self, built: object | None) -> None:
        self.built = built
        self.calls = 0

    async def build(self, **_: object) -> object | None:
        self.calls += 1
        return self.built


def _source(
    *,
    bars: tuple[CompletedOhlcvBar, ...],
    built: object | None,
    sizeable: bool = True,
    execution: tuple[CompletedOhlcvBar, ...] | None = None,
) -> tuple[ShadowContextSource, _Inputs]:
    inputs = _Inputs(built)
    source = ShadowContextSource(
        market_data=_Market(bars, execution=execution),  # type: ignore[arg-type]
        inputs=inputs,  # type: ignore[arg-type]
        risk=_risk_contexts() if sizeable else _NoRisk(),  # type: ignore[arg-type]
    )
    return source, inputs


@pytest.mark.asyncio
async def test_no_bars_is_no_evaluation() -> None:
    source, _ = _source(bars=(), built=object())

    assert await source.context_for(NOW) is None
    assert source.watermark is None


@pytest.mark.asyncio
async def test_a_pass_that_cannot_measure_leaves_the_watermark_alone() -> None:
    """Advancing it would skip that bar for good over a window that was
    briefly too thin, and the bar would never be evaluated."""
    source, inputs = _source(bars=_bars(20), built=None)

    assert await source.context_for(NOW) is None
    assert source.watermark is None
    assert inputs.calls == 1

    # The same bar is tried again rather than abandoned.
    assert await source.context_for(NOW + timedelta(seconds=30)) is None
    assert inputs.calls == 2


@pytest.mark.asyncio
async def test_one_evaluation_per_bar() -> None:
    """Re-running a bar records a second decision for evidence that has not
    changed, and the promotion evidence counts decisions."""
    bars = _bars(20)
    source, inputs = _source(bars=bars, built=_built())

    first = await source.context_for(NOW)
    second = await source.context_for(NOW + timedelta(seconds=30))

    assert first is not None
    assert second is None
    assert source.watermark == bars[-1].timestamp
    assert inputs.calls == 1


@pytest.mark.asyncio
async def test_a_new_bar_evaluates_again() -> None:
    bars = _bars(20)
    source, inputs = _source(bars=bars, built=_built())
    await source.context_for(NOW)

    source._market_data.bars = _bars(20, end=NOW + timedelta(minutes=5))  # type: ignore[attr-defined]
    again = await source.context_for(NOW + timedelta(minutes=5))

    assert again is not None
    assert inputs.calls == 2


@pytest.mark.asyncio
async def test_no_risk_context_stops_before_the_inputs_are_built() -> None:
    """Building them reaches the venue, and a pass that cannot size an order
    has no use for the answer."""
    source, inputs = _source(bars=_bars(20), built=_built(), sizeable=False)

    assert await source.context_for(NOW) is None
    assert inputs.calls == 0


def _execution_bars(
    count: int, *, end: datetime = NOW
) -> tuple[CompletedOhlcvBar, ...]:
    """Thirty-second bars ending where the five-minute series ends."""
    return tuple(
        CompletedOhlcvBar(
            timestamp=end - timedelta(seconds=30 * (count - index)),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1"),
        )
        for index in range(count)
    )


@pytest.mark.asyncio
async def test_the_pass_asks_for_thirty_second_bars_and_carries_them() -> None:
    """Section 4.2 confirms exhaustion there, so the pass has to fetch it.

    The depth is asserted because it is what the call costs: two hours is
    84,531 prints and 7 seconds of MySQL, against a day's 1.8 million and
    145 seconds. The plan's section 33.13 measured both.
    """
    execution = _execution_bars(240)
    source, _ = _source(bars=_bars(20), built=_built(), execution=execution)

    context = await source.context_for(NOW)

    assert context is not None
    market = source._market_data  # type: ignore[attr-defined]
    assert timedelta(seconds=30) in market.asked
    index = market.asked.index(timedelta(seconds=30))
    assert market.asked_history[index] == timedelta(hours=2)
    assert context.inputs.bars["30s"] == execution


@pytest.mark.asyncio
async def test_a_pass_with_no_tape_still_assembles() -> None:
    """An empty tape is ordinary - a restart, a lease handover.

    The key is carried anyway, so the bundle records BARS_30S_UNAVAILABLE
    rather than staying silent about a series it went looking for.
    """
    source, _ = _source(bars=_bars(20), built=_built())

    context = await source.context_for(NOW)

    assert context is not None
    assert context.inputs.bars["30s"] == ()
