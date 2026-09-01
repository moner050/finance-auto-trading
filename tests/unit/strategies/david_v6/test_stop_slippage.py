"""Modelling what a stop fills at, without ever having placed one.

The honest measurement is the ninety-fifth percentile of realised adverse
slippage, and that needs stops that actually filled. A Shadow loop places
nothing, Shadow has to run before Paper and Paper before LIVE, so measuring
it first is a circle. It is ranked off the venue's own bars instead.

What matters in these is the direction of the errors. The model must grow
when the market moves fast, because that is when a stop triggers, and it must
say nothing rather than something small when the tape is too short to rank.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.strategies.david_v6.costs import (
    MINIMUM_EXCURSION_BARS,
    STOP_SLIPPAGE_QUANTILE,
    adverse_excursions,
    stop_slippage_from_bars,
)

START = datetime(2026, 9, 1, tzinfo=UTC)


def _bar(index: int, *, high: str, low: str, close: str) -> CompletedOhlcvBar:
    return CompletedOhlcvBar(
        timestamp=START + timedelta(seconds=30 * index),
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(1),
    )


def _calm(count: int) -> tuple[CompletedOhlcvBar, ...]:
    return tuple(
        _bar(index, high="101", low="99", close="100") for index in range(count)
    )


def test_the_excursion_is_measured_past_the_last_known_price() -> None:
    """A stop is exposed to how far the market keeps going after reaching it,
    and within one bar that is bounded by the travel beyond the last close."""
    bars = (
        _bar(0, high="100", low="100", close="100"),
        _bar(1, high="103", low="97", close="100"),
    )

    assert adverse_excursions(bars) == (Decimal(3),)


def test_the_worse_of_the_two_sides_is_kept() -> None:
    """A stop sits on one side, and which side is not known when the
    distribution is built."""
    bars = (
        _bar(0, high="100", low="100", close="100"),
        _bar(1, high="101", low="95", close="100"),
    )

    assert adverse_excursions(bars) == (Decimal(5),)


def test_a_bar_that_never_left_the_last_price_contributes_nothing() -> None:
    bars = (
        _bar(0, high="100", low="100", close="100"),
        _bar(1, high="100", low="100", close="100"),
    )

    assert adverse_excursions(bars) == (Decimal(0),)


def test_a_short_tape_says_nothing_rather_than_something_small() -> None:
    """Under the floor the percentile is the largest of a handful, which
    describes the sample. The cost model already knows what to do with an
    input it does not have."""
    assert stop_slippage_from_bars(_calm(MINIMUM_EXCURSION_BARS)) is None


def test_enough_bars_produce_a_distance_some_bar_actually_travelled() -> None:
    """Nearest rank, so the answer is never interpolated between two moves
    that did not happen."""
    calm = _calm(MINIMUM_EXCURSION_BARS + 1)

    slippage = stop_slippage_from_bars(calm)

    assert slippage == Decimal(1)


def test_a_fast_market_raises_it() -> None:
    """The error has to lean this way: stops trigger when price is moving, so
    a model that stayed flat through a fast tape would understate exactly when
    it mattered."""
    calm = _calm(MINIMUM_EXCURSION_BARS + 1)
    violent = (
        *calm,
        *(
            _bar(index, high="120", low="80", close="100")
            for index in range(
                MINIMUM_EXCURSION_BARS + 1, MINIMUM_EXCURSION_BARS * 2 + 2
            )
        ),
    )

    assert stop_slippage_from_bars(violent) > stop_slippage_from_bars(calm)


def test_the_quantile_is_the_documented_one() -> None:
    assert Decimal("0.95") == STOP_SLIPPAGE_QUANTILE


def test_bars_of_the_wrong_type_are_refused() -> None:
    with pytest.raises(TypeError, match="exact completed bars"):
        adverse_excursions((object(),))  # type: ignore[arg-type]
