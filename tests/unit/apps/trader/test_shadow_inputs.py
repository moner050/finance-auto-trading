"""Rebuilding the loop's inputs, or refusing the pass.

Most of what the loop reads is a measurement now, and measurements go stale.
The tests that matter are the ones where something cannot be measured: a pass
that assembled half a set and called it complete would put a decision on
record that was measured against a gap, and that decision is the evidence a
promotion is later granted on.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid7

import pytest

from autotrader.apps.trader.shadow_inputs import FixedFacts, LiveBinanceInputs
from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.strategies.david_v6.costs import FeeSchedule
from autotrader.strategies.david_v6.manifest import (
    V6_DESIGN_SHA256,
    V6_SOURCE_SHA256,
    V6Manifest,
    v6_configuration_hash,
)
from autotrader.strategies.david_v6.order_flow import TradePrint
from autotrader.strategies.david_v6.regime import PessimismInputs

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
WINDOW_START = NOW - timedelta(minutes=30)


class _Spreads:
    def __init__(self, value: str = "0.1") -> None:
        self.value = Decimal(value)
        self.reads = 0

    async def spread(self) -> Decimal:
        self.reads += 1
        return self.value


class _Pessimism:
    def __init__(self) -> None:
        self.asked: list[date] = []

    async def pessimism(self, *, through: date) -> PessimismInputs:
        self.asked.append(through)
        return PessimismInputs(
            completed_date=through,
            volatility_percentile=Decimal("0.5"),
            put_call_percentile=None,
            breadth_percentile=Decimal("0.5"),
        )


def _manifest() -> V6Manifest:
    return V6Manifest(
        id=uuid7(),
        strategy_version_id=uuid7(),
        source_sha256=V6_SOURCE_SHA256,
        design_sha256=V6_DESIGN_SHA256,
        configuration_hash=v6_configuration_hash(),
        registered_at=NOW.replace(microsecond=0),
    )


def _fixed() -> FixedFacts:
    return FixedFacts(
        instrument_id=uuid7(),
        manifest=_manifest(),
        fee_schedule=FeeSchedule(
            entry_fee_per_unit=Decimal("30"),
            exit_taker_fee_per_unit=Decimal("30"),
        ),
        tick_size=Decimal("0.1"),
        minimum_quantity=Decimal("0.001"),
    )


def _bars(count: int) -> tuple[CompletedOhlcvBar, ...]:
    return tuple(
        CompletedOhlcvBar(
            timestamp=NOW - timedelta(minutes=5 * (count - index)),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("10"),
        )
        for index in range(count)
    )


def _trades(count: int) -> tuple[TradePrint, ...]:
    return tuple(
        TradePrint(
            provider_trade_id=f"t{index}",
            occurred_at=WINDOW_START + timedelta(seconds=30 * index + 1),
            price=Decimal("100") + Decimal(index % 3),
            quantity=Decimal("1"),
            buyer_maker=index % 2 == 0,
        )
        for index in range(count)
    )


def _provider(spreads: _Spreads, pessimism: _Pessimism) -> LiveBinanceInputs:
    return LiveBinanceInputs(fixed=_fixed(), spreads=spreads, pessimism=pessimism)


async def _build(
    provider: LiveBinanceInputs,
    *,
    bars: int = 40,
    daily: int = 5,
    trades: int = 40,
) -> object:
    return await provider.build(
        bars=_bars(bars),
        daily=_bars(daily),
        trades=_trades(trades),
        window_start=WINDOW_START,
        now=NOW,
    )


@pytest.mark.asyncio
async def test_a_complete_pass_measures_everything_it_reports() -> None:
    spreads, pessimism = _Spreads(), _Pessimism()

    built = await _build(_provider(spreads, pessimism))

    assert built is not None
    assert built.spread == Decimal("0.1")  # type: ignore[attr-defined]
    assert built.stop_slippage_q95 > 0  # type: ignore[attr-defined]
    assert built.order_flow_thresholds.atr_30s > 0  # type: ignore[attr-defined]
    assert spreads.reads == 1
    assert pessimism.asked == [NOW.date()]


@pytest.mark.asyncio
async def test_a_pass_that_cannot_rank_slippage_produces_nothing() -> None:
    """Not a smaller number: the pass does not happen. A decision measured
    against a gap is the evidence a promotion is later granted on."""
    built = await _build(_provider(_Spreads(), _Pessimism()), bars=5)

    assert built is None


@pytest.mark.asyncio
async def test_a_pass_without_a_thirty_second_atr_produces_nothing() -> None:
    """The order-flow rules measure progress against it, and without one they
    would be comparing against zero."""
    built = await _build(_provider(_Spreads(), _Pessimism()), trades=3)

    assert built is None


@pytest.mark.asyncio
async def test_a_pass_without_daily_history_produces_nothing() -> None:
    """Section 2.1's regime is taken over the instrument's own returns, and a
    single close yields none."""
    built = await _build(_provider(_Spreads(), _Pessimism()), daily=1)

    assert built is None


@pytest.mark.asyncio
async def test_the_measurements_are_re_read_every_pass() -> None:
    """Holding one set for the life of a run means evaluating this bar
    against an hour-old spread."""
    spreads, pessimism = _Spreads(), _Pessimism()
    provider = _provider(spreads, pessimism)

    await _build(provider)
    spreads.value = Decimal("0.5")
    second = await _build(provider)

    assert second is not None
    assert second.spread == Decimal("0.5")  # type: ignore[attr-defined]
    assert spreads.reads == 2


@pytest.mark.asyncio
async def test_the_session_calendar_follows_the_close_hour() -> None:
    """20:00 UTC cuts the day, so noon belongs to the session that opened the
    day before."""
    built = await _build(_provider(_Spreads(), _Pessimism()))

    assert built is not None
    assert built.calendar.session_date == date(2026, 8, 31)  # type: ignore[attr-defined]
