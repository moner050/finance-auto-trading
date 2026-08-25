from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.strategies.david_v6.pivots import (
    DivergenceKind,
    PivotConfig,
    PivotKind,
    confirmed_pivots,
    evaluate_divergence,
)

START = datetime(2026, 8, 24, tzinfo=UTC)


def _bars(
    lows: tuple[int, ...], highs: tuple[int, ...] | None = None
) -> tuple[CompletedOhlcvBar, ...]:
    selected_highs = (20,) * len(lows) if highs is None else highs
    return tuple(
        CompletedOhlcvBar(
            timestamp=START + timedelta(minutes=index),
            open=Decimal(low),
            high=Decimal(high),
            low=Decimal(low),
            close=Decimal(low),
            volume=Decimal("1"),
        )
        for index, (low, high) in enumerate(zip(lows, selected_highs, strict=True))
    )


def test_confirmed_pivots_do_not_expose_a_provisional_final_extreme() -> None:
    bars = _bars((10, 10, 10, 8, 9, 10, 7))

    pivots = confirmed_pivots(bars, PivotConfig(left=3, right=1))

    assert tuple((pivot.index, pivot.kind) for pivot in pivots) == ((3, PivotKind.LOW),)


def test_regular_and_hidden_divergence_are_distinct_facts() -> None:
    regular_bars = _bars((10, 10, 10, 8, 9, 10, 7))
    regular_oscillator = (
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        Decimal("-2"),
        Decimal("0"),
        Decimal("0"),
        Decimal("-1"),
    )
    hidden_bars = _bars((10, 10, 10, 8, 12, 11, 10, 9))
    hidden_oscillator = (
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        Decimal("-1"),
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        Decimal("-2"),
    )

    regular = evaluate_divergence(regular_bars, regular_oscillator)
    hidden = evaluate_divergence(hidden_bars, hidden_oscillator)

    assert tuple(item.kind for item in regular.regular) == (
        DivergenceKind.REGULAR_BULLISH,
    )
    assert regular.hidden == ()
    assert tuple(item.kind for item in hidden.hidden) == (
        DivergenceKind.HIDDEN_BULLISH,
    )
    assert hidden.regular == ()
