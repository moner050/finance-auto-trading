"""Reading the direction off the divergence instead of assuming one.

Every evaluation this system ever made was a BUY evaluation, because
`BinanceRiskContexts` defaulted the side and no caller passed one. Section 21
lists `permanent_long_only` as prohibited and that is what the default came
to: a bearish setup could not be seen at all, and the passes that recorded
DIRECTION_EVIDENCE_MISSING never asked whether a short was there.

The case worth guarding is a window carrying both a bullish and a bearish
divergence, because picking either side then finds its own evidence and looks
supported.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import Side
from autotrader.strategies.david_v6.direction import (
    aligned_macd_histogram,
    divergence_directions,
    divergence_side,
    single_direction,
)
from autotrader.strategies.david_v6.metodo import MACD_WARMUP_BARS

START = datetime(2026, 8, 20, tzinfo=UTC)
FIVE_MINUTES = timedelta(minutes=5)

# A decelerating move: each leg smaller than the last, so price keeps making
# new extremes while momentum stops confirming them.
FADING = ("6", "4.5", "3.2", "2.2", "1.4", "0.9", "0.5", "0.3", "0.2", "0.1")


def _bars(closes: list[Decimal]) -> tuple[CompletedOhlcvBar, ...]:
    return tuple(
        CompletedOhlcvBar(
            timestamp=START + FIVE_MINUTES * index,
            open=close,
            high=close + Decimal("0.5"),
            low=close - Decimal("0.5"),
            close=close,
            volume=Decimal(100),
        )
        for index, close in enumerate(closes)
    )


def _baseline(count: int = 400) -> list[Decimal]:
    """Oscillation, only to give MACD enough history to have a value."""
    return [
        Decimal(120) + Decimal((index * 7) % 11) - Decimal(5) for index in range(count)
    ]


def _fading_decline() -> tuple[CompletedOhlcvBar, ...]:
    """New lows on fading momentum: one regular bullish divergence."""
    closes = _baseline()
    price = closes[-1]
    for step in FADING:
        for _ in range(4):
            price -= Decimal(step) / Decimal(4)
            closes.append(price)
    return _bars(closes)


def _swing_then_fading_decline() -> tuple[CompletedOhlcvBar, ...]:
    """A rally before the decline, which leaves a bearish signal standing too.

    Both readings are present at once and the window says two things.
    """
    closes = _baseline()
    closes += [Decimal(120) + Decimal(index) * Decimal("0.5") for index in range(40)]
    price = closes[-1]
    for step in FADING:
        for _ in range(4):
            price -= Decimal(step) / Decimal(4)
            closes.append(price)
    return _bars(closes)


def test_a_falling_market_that_is_running_out_points_long() -> None:
    assert divergence_directions(_fading_decline()) == frozenset({Side.BUY})
    assert divergence_side(_fading_decline()) is Side.BUY


def test_a_bearish_signal_maps_to_the_short_side() -> None:
    """The half of the strategy that was unreachable.

    Proven through the mixed window rather than a bearish-only one: no
    synthetic window produced a lone bearish divergence, and inventing bars
    until one appeared would be fitting the fixture to the assertion.
    """
    assert Side.SELL in divergence_directions(_swing_then_fading_decline())


def test_a_window_pointing_both_ways_yields_no_side() -> None:
    """Picking one would find its own divergence and look supported, which is
    what CONTRADICTORY_DIRECTION_EVIDENCE exists to prevent."""
    bars = _swing_then_fading_decline()

    assert divergence_directions(bars) == frozenset({Side.BUY, Side.SELL})
    assert divergence_side(bars) is None


def test_no_divergence_yields_no_side() -> None:
    assert single_direction(frozenset()) is None


def test_one_direction_is_taken() -> None:
    assert single_direction(frozenset({Side.SELL})) is Side.SELL
    assert single_direction(frozenset({Side.BUY})) is Side.BUY


def test_a_window_too_short_for_macd_has_no_direction() -> None:
    """A statement about the window, not about the market."""
    short = _bars(_baseline(MACD_WARMUP_BARS - 5))

    assert aligned_macd_histogram(short) is None
    assert divergence_directions(short) == frozenset()
    assert divergence_side(short) is None


def test_the_alignment_keeps_bars_and_histogram_the_same_length() -> None:
    """They are indexed together downstream; a warm-up dropped from one and
    not the other silently shifts every divergence by the offset."""
    aligned = aligned_macd_histogram(_fading_decline())

    assert aligned is not None
    bars, histogram = aligned
    assert len(bars) == len(histogram)
    assert len(bars) > 0
