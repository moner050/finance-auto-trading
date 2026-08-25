from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from typing import cast

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import Side
from autotrader.strategies.david_v6.pivots import Pivot, PivotKind
from autotrader.strategies.david_v6.zones import ZoneFacts


@dataclass(frozen=True, slots=True)
class ExhaustionSequence:
    direction: Side
    confirmed: bool
    research_only: bool
    history: tuple[Pivot, ...]
    evaluation_pivots: tuple[Pivot, ...]
    structural_reference_price: Decimal
    confirmed_at: datetime


@dataclass(frozen=True, slots=True)
class ExhaustionFacts:
    observed_at: datetime | None
    bullish: ExhaustionSequence | None
    bearish: ExhaustionSequence | None


def evaluate_exhaustion(
    bars: Sequence[CompletedOhlcvBar],
    *,
    zones: ZoneFacts,
    pivots: Sequence[Pivot],
) -> ExhaustionFacts:
    values = tuple(bars)
    if any(type(bar) is not CompletedOhlcvBar for bar in values):
        raise TypeError("exhaustion requires exact completed OHLCV bars")
    if any(later.timestamp <= earlier.timestamp for earlier, later in pairwise(values)):
        raise ValueError("exhaustion bars must be strictly ascending")
    if type(cast(object, zones)) is not ZoneFacts:
        raise TypeError("zones must be exact ZoneFacts")
    pivot_values = tuple(pivots)
    if any(type(pivot) is not Pivot for pivot in pivot_values):
        raise TypeError("pivots must contain exact Pivot values")
    for pivot in pivot_values:
        _require_pivot_matches_bar(pivot, values)
    return ExhaustionFacts(
        observed_at=values[-1].timestamp if values else None,
        bullish=_sequence(
            bars=values,
            zones=zones,
            pivots=pivot_values,
            kind=PivotKind.LOW,
        ),
        bearish=_sequence(
            bars=values,
            zones=zones,
            pivots=pivot_values,
            kind=PivotKind.HIGH,
        ),
    )


def _sequence(
    *,
    bars: tuple[CompletedOhlcvBar, ...],
    zones: ZoneFacts,
    pivots: tuple[Pivot, ...],
    kind: PivotKind,
) -> ExhaustionSequence | None:
    selected = tuple(
        sorted(
            (pivot for pivot in pivots if pivot.confirmed and pivot.kind is kind),
            key=lambda pivot: pivot.index,
        )
    )
    if len(selected) < 2:
        return None
    history: list[Pivot] = [selected[0]]
    for previous, current in pairwise(selected):
        price_extends = (
            current.price < previous.price
            if kind is PivotKind.LOW
            else current.price > previous.price
        )
        volume_decreases = bars[current.index].volume < bars[previous.index].volume
        if price_extends and volume_decreases:
            history.append(current)
        else:
            history = [current]
    if len(history) < 2 or not _inside_zone(history[-1].price, zones):
        return None
    pivot_history = tuple(history)
    last = pivot_history[-1]
    confirmed = len(pivot_history) >= 3
    return ExhaustionSequence(
        direction=Side.BUY if kind is PivotKind.LOW else Side.SELL,
        confirmed=confirmed,
        research_only=not confirmed,
        history=pivot_history,
        evaluation_pivots=pivot_history[-4:],
        structural_reference_price=last.price,
        confirmed_at=bars[last.confirmation_index].timestamp,
    )


def _inside_zone(price: Decimal, facts: ZoneFacts) -> bool:
    return any(
        zone.lower_boundary <= price <= zone.upper_boundary for zone in facts.zones
    )


def _require_pivot_matches_bar(
    pivot: Pivot, bars: tuple[CompletedOhlcvBar, ...]
) -> None:
    if (
        pivot.index >= len(bars)
        or pivot.confirmation_index >= len(bars)
        or pivot.timestamp != bars[pivot.index].timestamp
    ):
        raise ValueError("pivot does not match completed bar history")
    expected_price = (
        bars[pivot.index].low if pivot.kind is PivotKind.LOW else bars[pivot.index].high
    )
    if pivot.price != expected_price:
        raise ValueError("pivot price does not match completed bar")


__all__ = (
    "ExhaustionFacts",
    "ExhaustionSequence",
    "evaluate_exhaustion",
)
