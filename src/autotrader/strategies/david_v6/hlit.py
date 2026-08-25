"""HLIT Fibonacci anchors and retracement levels.

Implements section 3 of the David Trullas Vila v6 specification. A regular
MACD divergence is the precondition: without it no anchor is drawn and no
level exists, which is the document's first invariant.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from typing import cast

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import Side
from autotrader.shared.decimal import require_decimal
from autotrader.strategies.david_v6.pivots import (
    DivergenceFacts,
    DivergenceKind,
    DivergenceSignal,
)

FIB_25 = Decimal("0.25")
FIB_50 = Decimal("0.50")
FIB_66 = Decimal("0.66")
FIB_LEVELS = (FIB_25, FIB_50, FIB_66)
TARGET_LEVEL = FIB_66


@dataclass(frozen=True, slots=True)
class HlitSetup:
    """One drawn retracement, always sourced from a regular divergence."""

    direction: Side
    divergence_kind: DivergenceKind
    anchor_a: Decimal
    anchor_b: Decimal
    fib_25: Decimal
    fib_50: Decimal
    fib_66: Decimal
    target_price: Decimal
    invalidation_price: Decimal
    first_pivot_index: int
    second_pivot_index: int
    observed_at: datetime

    def __post_init__(self) -> None:
        if type(self.direction) is not Side:
            raise TypeError("direction must be an exact Side")
        if type(self.divergence_kind) is not DivergenceKind:
            raise TypeError("divergence_kind must be an exact DivergenceKind")
        for name in (
            "anchor_a",
            "anchor_b",
            "fib_25",
            "fib_50",
            "fib_66",
            "target_price",
            "invalidation_price",
        ):
            value = require_decimal(getattr(self, name))
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        if (
            type(self.first_pivot_index) is not int
            or type(self.second_pivot_index) is not int
            or self.first_pivot_index < 0
            or self.second_pivot_index <= self.first_pivot_index
        ):
            raise ValueError("HLIT pivots must be ordered non-negative indices")
        if self.target_price != self.fib_66:
            raise ValueError("HLIT target must be the 66 percent level")
        if self.invalidation_price != self.anchor_b:
            raise ValueError("HLIT invalidation must be the extreme anchor")
        if self.direction is Side.BUY:
            if not self.anchor_b < self.fib_25 < self.fib_50 < self.fib_66:
                raise ValueError("bullish HLIT levels must ascend from the low anchor")
            if self.fib_66 >= self.anchor_a:
                raise ValueError("bullish 66 percent must stay below the high anchor")
        elif not self.anchor_b > self.fib_25 > self.fib_50 > self.fib_66:
            raise ValueError("bearish HLIT levels must descend from the high anchor")
        elif self.fib_66 <= self.anchor_a:
            raise ValueError("bearish 66 percent must stay above the low anchor")


@dataclass(frozen=True, slots=True)
class HlitFacts:
    observed_at: datetime | None
    bullish: HlitSetup | None
    bearish: HlitSetup | None

    def __post_init__(self) -> None:
        for name in ("bullish", "bearish"):
            value = getattr(self, name)
            if value is not None and type(value) is not HlitSetup:
                raise TypeError(f"{name} must be an exact HlitSetup")

    def for_side(self, side: Side) -> HlitSetup | None:
        if type(side) is not Side:
            raise TypeError("side must be an exact Side")
        return self.bullish if side is Side.BUY else self.bearish


def build_hlit_setups(
    bars: Sequence[CompletedOhlcvBar],
    divergence: DivergenceFacts,
) -> HlitFacts:
    """Draw the 25/50/66 retracement for each regular divergence.

    Hidden divergences never produce a setup: the specification allows drawing
    only when the regular asymmetry holds.
    """
    if type(cast(object, divergence)) is not DivergenceFacts:
        raise TypeError("divergence must be exact DivergenceFacts")
    values = _bars(bars)
    return HlitFacts(
        observed_at=values[-1].timestamp if values else None,
        bullish=_setup(values, divergence, DivergenceKind.REGULAR_BULLISH),
        bearish=_setup(values, divergence, DivergenceKind.REGULAR_BEARISH),
    )


def _setup(
    bars: tuple[CompletedOhlcvBar, ...],
    divergence: DivergenceFacts,
    kind: DivergenceKind,
) -> HlitSetup | None:
    signal = next(
        (candidate for candidate in divergence.regular if candidate.kind is kind),
        None,
    )
    if signal is None:
        return None
    segment = _segment(bars, signal)
    if segment is None:
        return None
    if kind is DivergenceKind.REGULAR_BULLISH:
        anchor_a = max(bar.high for bar in segment)
        anchor_b = signal.second.price
        span = anchor_a - anchor_b
        if span <= 0:
            return None
        levels = tuple(anchor_b + span * level for level in FIB_LEVELS)
        direction = Side.BUY
    else:
        anchor_a = min(bar.low for bar in segment)
        anchor_b = signal.second.price
        span = anchor_b - anchor_a
        if span <= 0:
            return None
        levels = tuple(anchor_b - span * level for level in FIB_LEVELS)
        direction = Side.SELL
    fib_25, fib_50, fib_66 = levels
    return HlitSetup(
        direction=direction,
        divergence_kind=kind,
        anchor_a=anchor_a,
        anchor_b=anchor_b,
        fib_25=fib_25,
        fib_50=fib_50,
        fib_66=fib_66,
        target_price=fib_66,
        invalidation_price=anchor_b,
        first_pivot_index=signal.first.index,
        second_pivot_index=signal.second.index,
        observed_at=bars[-1].timestamp,
    )


def _segment(
    bars: tuple[CompletedOhlcvBar, ...],
    signal: DivergenceSignal,
) -> tuple[CompletedOhlcvBar, ...] | None:
    first = signal.first.index
    second = signal.second.index
    if second >= len(bars) or first >= second:
        return None
    if signal.first.timestamp != bars[first].timestamp:
        return None
    if signal.second.timestamp != bars[second].timestamp:
        return None
    return bars[first : second + 1]


def _bars(values: Sequence[CompletedOhlcvBar]) -> tuple[CompletedOhlcvBar, ...]:
    bars = tuple(values)
    if any(type(bar) is not CompletedOhlcvBar for bar in bars):
        raise TypeError("HLIT anchors require exact completed OHLCV bars")
    if any(later.timestamp <= earlier.timestamp for earlier, later in pairwise(bars)):
        raise ValueError("HLIT bars must be strictly ascending")
    return bars


__all__ = (
    "FIB_25",
    "FIB_50",
    "FIB_66",
    "FIB_LEVELS",
    "TARGET_LEVEL",
    "HlitFacts",
    "HlitSetup",
    "build_hlit_setups",
)
