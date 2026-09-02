"""Which way the setup points, decided by the divergence and nothing else.

Section 3 puts this first: find the asymmetry where price makes a new extreme
and MACD does not, and if it is not there, do not even draw the fibonacci. So
direction is read off the divergence rather than chosen and then checked.

The engine only checks. `run_pass` hands it a side and it verifies that the
matching direction evidence exists, which means the side has to be decided
before the risk context is built. It was not: `BinanceRiskContexts` defaulted
to BUY and nothing overrode it, so every evaluation the system ever made asked
"is a long on?" and a bearish setup could not be seen at all. Section 21 lists
`permanent_long_only` as prohibited, and that is what the default amounted to.

The MACD alignment lives here rather than in the caller because the assembly
needs the same series for its own divergence evidence. Two computations of the
same thing are two chances to disagree, and a source that picked SELL from one
alignment while the assembly matched BUY from another would produce a decision
refusing itself for reasons nobody could reconstruct.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import cast

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import Side
from autotrader.strategies.david_v6.metodo import MACD_WARMUP_BARS, macd_series
from autotrader.strategies.david_v6.pivots import (
    DivergenceFacts,
    DivergenceKind,
    evaluate_divergence,
)

_SIDE_OF = {
    DivergenceKind.REGULAR_BULLISH: Side.BUY,
    DivergenceKind.REGULAR_BEARISH: Side.SELL,
}


def aligned_macd_histogram(
    bars: Sequence[CompletedOhlcvBar],
) -> tuple[tuple[CompletedOhlcvBar, ...], tuple[Decimal, ...]] | None:
    """Bars and histogram with the warm-up prefix dropped from both.

    None when there is not enough history for MACD to have a value, which is
    a statement about the window rather than about the market.
    """
    values = tuple(bars)
    if len(values) < MACD_WARMUP_BARS:
        return None
    closes = tuple(bar.close for bar in values)
    macd, signal = macd_series(closes)
    start = next(
        (
            index
            for index in range(len(closes))
            if macd[index] is not None and signal[index] is not None
        ),
        None,
    )
    if start is None:
        return None
    histogram = tuple(
        cast(Decimal, macd[index]) - cast(Decimal, signal[index])
        for index in range(start, len(closes))
    )
    return values[start:], histogram


def regular_divergence(bars: Sequence[CompletedOhlcvBar]) -> DivergenceFacts | None:
    aligned = aligned_macd_histogram(bars)
    if aligned is None:
        return None
    return evaluate_divergence(*aligned)


def divergence_directions(bars: Sequence[CompletedOhlcvBar]) -> frozenset[Side]:
    """Which sides the regular divergences point to. Empty, one, or both.

    The three answers are not interchangeable and callers must not collapse
    them, which is why this returns the set rather than a side.

    Empty is section 3's "그 비대칭이 없으면 피보나치를 그리지도 않는다". No
    setup exists, and neither side can pass, so which side a refusal is
    recorded under changes nothing about the outcome.

    Both is a market saying two things at once. Picking one would slip past
    `CONTRADICTORY_DIRECTION_EVIDENCE`, which exists to stop a decision built
    on evidence that disagrees with itself - and the chosen side would find
    its matching divergence and look supported. Preferring the more recent,
    or the stronger, would be a rule the author never wrote.
    """
    facts = regular_divergence(bars)
    if facts is None:
        return frozenset()
    return frozenset(
        _SIDE_OF[signal.kind] for signal in facts.regular if signal.kind in _SIDE_OF
    )


def single_direction(directions: frozenset[Side]) -> Side | None:
    """One side, or None for none and for both.

    Separate from the reading of it so the rule can be stated against all
    three cases directly. A window carrying both divergences is rare enough
    that a test built from synthetic bars would mostly be testing whether the
    bars happened to produce one.
    """
    return next(iter(directions)) if len(directions) == 1 else None


def divergence_side(bars: Sequence[CompletedOhlcvBar]) -> Side | None:
    """The single side the divergence implies, or None for empty or both."""
    return single_direction(divergence_directions(bars))


__all__ = (
    "aligned_macd_histogram",
    "divergence_directions",
    "divergence_side",
    "regular_divergence",
    "single_direction",
)
