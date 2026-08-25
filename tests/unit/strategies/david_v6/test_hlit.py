from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import Side
from autotrader.strategies.david_v6.hlit import (
    FIB_LEVELS,
    TARGET_LEVEL,
    build_hlit_setups,
)
from autotrader.strategies.david_v6.pivots import (
    DivergenceFacts,
    DivergenceKind,
    DivergenceSignal,
    Pivot,
)
from autotrader.strategies.david_v6.pivots import PivotKind as Kind

START = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)

# index:            0     1     2      3     4     5      6
_HIGHS = ("101", "100", "110", "108", "104", "99", "100")
_LOWS = ("100", "98", "104", "90", "100", "96", "99")


def _bars() -> tuple[CompletedOhlcvBar, ...]:
    return tuple(
        CompletedOhlcvBar(
            timestamp=START + timedelta(minutes=5 * index),
            open=Decimal(low),
            high=Decimal(high),
            low=Decimal(low),
            close=Decimal(high),
            volume=Decimal(100 - index),
        )
        for index, (high, low) in enumerate(zip(_HIGHS, _LOWS, strict=True))
    )


def _pivot(index: int, kind: Kind, price: str) -> Pivot:
    return Pivot(
        index=index,
        confirmation_index=index,
        kind=kind,
        price=Decimal(price),
        timestamp=START + timedelta(minutes=5 * index),
        confirmed=True,
    )


def _bullish() -> DivergenceSignal:
    return DivergenceSignal(
        kind=DivergenceKind.REGULAR_BULLISH,
        first=_pivot(1, Kind.LOW, "98"),
        second=_pivot(5, Kind.LOW, "96"),
        first_oscillator=Decimal("-3"),
        second_oscillator=Decimal("-1"),
    )


def _bearish() -> DivergenceSignal:
    return DivergenceSignal(
        kind=DivergenceKind.REGULAR_BEARISH,
        first=_pivot(1, Kind.HIGH, "100"),
        second=_pivot(5, Kind.HIGH, "99"),
        first_oscillator=Decimal("3"),
        second_oscillator=Decimal("1"),
    )


def _facts(
    *,
    regular: tuple[DivergenceSignal, ...] = (),
    hidden: tuple[DivergenceSignal, ...] = (),
) -> DivergenceFacts:
    return DivergenceFacts(observed_at=START, regular=regular, hidden=hidden)


def test_levels_are_the_documented_twenty_five_fifty_and_sixty_six() -> None:
    assert (Decimal("0.25"), Decimal("0.50"), Decimal("0.66")) == FIB_LEVELS
    assert Decimal("0.66") == TARGET_LEVEL


def test_bullish_anchor_is_the_absolute_high_between_the_two_lows() -> None:
    facts = build_hlit_setups(_bars(), _facts(regular=(_bullish(),)))

    setup = facts.bullish
    assert setup is not None
    # bars[1..5] highs are 100, 110, 108, 104, 99: the absolute high is 110.
    assert setup.anchor_a == Decimal("110")
    assert setup.anchor_b == Decimal("96")
    assert setup.direction is Side.BUY


def test_bullish_levels_retrace_upward_from_the_low_anchor() -> None:
    facts = build_hlit_setups(_bars(), _facts(regular=(_bullish(),)))

    setup = facts.bullish
    assert setup is not None
    span = Decimal("110") - Decimal("96")
    assert setup.fib_25 == Decimal("96") + span * Decimal("0.25")
    assert setup.fib_50 == Decimal("96") + span * Decimal("0.50")
    assert setup.fib_66 == Decimal("96") + span * Decimal("0.66")
    assert setup.target_price == setup.fib_66
    assert setup.invalidation_price == Decimal("96")


def test_bearish_anchor_is_the_absolute_low_between_the_two_highs() -> None:
    facts = build_hlit_setups(_bars(), _facts(regular=(_bearish(),)))

    setup = facts.bearish
    assert setup is not None
    # bars[1..5] lows are 98, 104, 90, 100, 96: the absolute low is 90.
    assert setup.anchor_a == Decimal("90")
    assert setup.anchor_b == Decimal("99")
    assert setup.direction is Side.SELL


def test_bearish_levels_retrace_downward_from_the_high_anchor() -> None:
    facts = build_hlit_setups(_bars(), _facts(regular=(_bearish(),)))

    setup = facts.bearish
    assert setup is not None
    span = Decimal("99") - Decimal("90")
    assert setup.fib_25 == Decimal("99") - span * Decimal("0.25")
    assert setup.fib_50 == Decimal("99") - span * Decimal("0.50")
    assert setup.fib_66 == Decimal("99") - span * Decimal("0.66")
    assert setup.target_price == setup.fib_66


def test_absent_divergence_draws_nothing() -> None:
    facts = build_hlit_setups(_bars(), _facts())

    assert facts.bullish is None
    assert facts.bearish is None


def test_hidden_divergence_alone_draws_nothing() -> None:
    hidden = DivergenceSignal(
        kind=DivergenceKind.HIDDEN_BULLISH,
        first=_pivot(1, Kind.LOW, "98"),
        second=_pivot(5, Kind.LOW, "96"),
        first_oscillator=Decimal("-1"),
        second_oscillator=Decimal("-3"),
    )

    facts = build_hlit_setups(_bars(), _facts(hidden=(hidden,)))

    assert facts.bullish is None
    assert facts.bearish is None


def test_for_side_selects_the_matching_setup() -> None:
    facts = build_hlit_setups(_bars(), _facts(regular=(_bullish(), _bearish())))

    assert facts.for_side(Side.BUY) is facts.bullish
    assert facts.for_side(Side.SELL) is facts.bearish


def test_pivot_that_does_not_match_the_bar_history_draws_nothing() -> None:
    detached = DivergenceSignal(
        kind=DivergenceKind.REGULAR_BULLISH,
        first=Pivot(
            index=1,
            confirmation_index=1,
            kind=Kind.LOW,
            price=Decimal("98"),
            timestamp=START - timedelta(days=1),
            confirmed=True,
        ),
        second=_pivot(5, Kind.LOW, "96"),
        first_oscillator=Decimal("-3"),
        second_oscillator=Decimal("-1"),
    )

    facts = build_hlit_setups(_bars(), _facts(regular=(detached,)))

    assert facts.bullish is None


def test_pivot_index_beyond_the_bar_history_draws_nothing() -> None:
    beyond = DivergenceSignal(
        kind=DivergenceKind.REGULAR_BULLISH,
        first=_pivot(1, Kind.LOW, "98"),
        second=Pivot(
            index=99,
            confirmation_index=99,
            kind=Kind.LOW,
            price=Decimal("96"),
            timestamp=START + timedelta(minutes=495),
            confirmed=True,
        ),
        first_oscillator=Decimal("-3"),
        second_oscillator=Decimal("-1"),
    )

    facts = build_hlit_setups(_bars(), _facts(regular=(beyond,)))

    assert facts.bullish is None


def test_bars_must_be_strictly_ascending() -> None:
    bars = _bars()

    with pytest.raises(ValueError, match="strictly ascending"):
        build_hlit_setups((bars[1], bars[0]), _facts())


def test_divergence_must_be_exact_facts() -> None:
    with pytest.raises(TypeError, match="exact DivergenceFacts"):
        build_hlit_setups(_bars(), object())  # type: ignore[arg-type]
