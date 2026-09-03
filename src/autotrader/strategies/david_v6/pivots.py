from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import cast

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.shared.decimal import require_decimal


class PivotKind(StrEnum):
    HIGH = "HIGH"
    LOW = "LOW"


class DivergenceKind(StrEnum):
    REGULAR_BULLISH = "REGULAR_BULLISH"
    REGULAR_BEARISH = "REGULAR_BEARISH"
    HIDDEN_BULLISH = "HIDDEN_BULLISH"
    HIDDEN_BEARISH = "HIDDEN_BEARISH"


@dataclass(frozen=True, slots=True)
class PivotConfig:
    left: int = 3
    right: int = 0

    def __post_init__(self) -> None:
        if type(self.left) is not int or self.left <= 0:
            raise ValueError("pivot left must be positive")
        if type(self.right) is not int or self.right < 0:
            raise ValueError("pivot right must be non-negative")


@dataclass(frozen=True, slots=True)
class Pivot:
    index: int
    confirmation_index: int
    kind: PivotKind
    price: Decimal
    timestamp: datetime
    confirmed: bool

    def __post_init__(self) -> None:
        if (
            type(self.index) is not int
            or self.index < 0
            or type(self.confirmation_index) is not int
            or self.confirmation_index < self.index
        ):
            raise ValueError("pivot indices are invalid")
        if type(self.kind) is not PivotKind:
            raise TypeError("pivot kind must be exact PivotKind")
        price = require_decimal(self.price)
        if price <= 0:
            raise ValueError("pivot price must be positive")
        if type(self.confirmed) is not bool:
            raise TypeError("pivot confirmed must be bool")
        object.__setattr__(self, "price", price)


@dataclass(frozen=True, slots=True)
class DivergenceSignal:
    kind: DivergenceKind
    first: Pivot
    second: Pivot
    first_oscillator: Decimal
    second_oscillator: Decimal


@dataclass(frozen=True, slots=True)
class DivergenceFacts:
    observed_at: datetime | None
    regular: tuple[DivergenceSignal, ...]
    hidden: tuple[DivergenceSignal, ...]


def confirmed_pivots(
    bars: Sequence[CompletedOhlcvBar], config: PivotConfig
) -> tuple[Pivot, ...]:
    if type(cast(object, config)) is not PivotConfig:
        raise TypeError("config must be exact PivotConfig")
    values = _bars(bars)
    pivots: list[Pivot] = []
    for index in range(config.left, len(values) - config.right):
        candidate = values[index]
        neighbours = tuple(
            values[other]
            for other in range(index - config.left, index + config.right + 1)
            if other != index
        )
        confirmation_index = index + config.right
        if all(candidate.high > other.high for other in neighbours):
            pivots.append(
                Pivot(
                    index=index,
                    confirmation_index=confirmation_index,
                    kind=PivotKind.HIGH,
                    price=candidate.high,
                    timestamp=candidate.timestamp,
                    confirmed=True,
                )
            )
        if all(candidate.low < other.low for other in neighbours):
            pivots.append(
                Pivot(
                    index=index,
                    confirmation_index=confirmation_index,
                    kind=PivotKind.LOW,
                    price=candidate.low,
                    timestamp=candidate.timestamp,
                    confirmed=True,
                )
            )
    return tuple(pivots)


def evaluate_divergence(
    prices: Sequence[CompletedOhlcvBar],
    oscillator: Sequence[Decimal],
    config: PivotConfig | None = None,
) -> DivergenceFacts:
    """The last two pivots of each kind, and what the oscillator did between.

    `config` decides how large a swing has to be to count as one, which
    section 3.4 calls the matryoshka trade-off and treats as A0: the larger
    swing is the more reliable one and costs more when it fails. It was
    fixed here at the default and appears in neither of §13.2's lists, so
    §13.3's rule applies - a threshold we define ourselves is exposed as a
    parameter and sensitivity-analysed. The default is unchanged, so
    production behaves exactly as before.
    """
    bars = _bars(prices)
    oscillator_values = tuple(require_decimal(value) for value in oscillator)
    if len(oscillator_values) != len(bars):
        raise ValueError("oscillator must align with completed bars")
    pivots = confirmed_pivots(bars, config or PivotConfig())
    signals: list[DivergenceSignal] = []
    for kind in (PivotKind.LOW, PivotKind.HIGH):
        selected = tuple(pivot for pivot in pivots if pivot.kind is kind)
        if len(selected) < 2:
            continue
        first, second = selected[-2:]
        first_oscillator = oscillator_values[first.index]
        second_oscillator = oscillator_values[second.index]
        divergence = _kind(
            pivot_kind=kind,
            first_price=first.price,
            second_price=second.price,
            first_oscillator=first_oscillator,
            second_oscillator=second_oscillator,
        )
        if divergence is not None:
            signals.append(
                DivergenceSignal(
                    kind=divergence,
                    first=first,
                    second=second,
                    first_oscillator=first_oscillator,
                    second_oscillator=second_oscillator,
                )
            )
    return DivergenceFacts(
        observed_at=bars[-1].timestamp if bars else None,
        regular=tuple(
            signal for signal in signals if signal.kind.value.startswith("REGULAR_")
        ),
        hidden=tuple(
            signal for signal in signals if signal.kind.value.startswith("HIDDEN_")
        ),
    )


def _bars(values: Sequence[CompletedOhlcvBar]) -> tuple[CompletedOhlcvBar, ...]:
    bars = tuple(values)
    if any(type(bar) is not CompletedOhlcvBar for bar in bars):
        raise TypeError("pivots require exact completed OHLCV bars")
    if any(later.timestamp <= earlier.timestamp for earlier, later in pairwise(bars)):
        raise ValueError("pivot bars must be strictly ascending")
    return bars


def _kind(
    *,
    pivot_kind: PivotKind,
    first_price: Decimal,
    second_price: Decimal,
    first_oscillator: Decimal,
    second_oscillator: Decimal,
) -> DivergenceKind | None:
    if pivot_kind is PivotKind.LOW:
        if second_price < first_price and second_oscillator > first_oscillator:
            return DivergenceKind.REGULAR_BULLISH
        if second_price > first_price and second_oscillator < first_oscillator:
            return DivergenceKind.HIDDEN_BULLISH
    else:
        if second_price > first_price and second_oscillator < first_oscillator:
            return DivergenceKind.REGULAR_BEARISH
        if second_price < first_price and second_oscillator > first_oscillator:
            return DivergenceKind.HIDDEN_BEARISH
    return None


__all__ = (
    "DivergenceFacts",
    "DivergenceKind",
    "DivergenceSignal",
    "Pivot",
    "PivotConfig",
    "PivotKind",
    "confirmed_pivots",
    "evaluate_divergence",
)
