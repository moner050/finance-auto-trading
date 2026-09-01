from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from itertools import pairwise

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.shared.decimal import require_decimal
from autotrader.strategies.david_v6.models import EvidenceState

# How far a stop is expected to fill past its trigger, and where that number
# comes from when no order has ever been placed.
#
# The honest measurement is the ninety-fifth percentile of realised adverse
# slippage, and that needs a history of stops actually filling. A Shadow loop
# places nothing, and Shadow is what has to run before Paper, which has to run
# before LIVE: measuring it first is a circle.
#
# So it is modelled from the tape, the same way breadth, volatility, the Big
# Trade threshold and the extreme delta are - ranked against the venue's own
# recent bars rather than typed in. The quantity ranked is how far price ran
# past the last price it was known at within one bar, which is the exposure a
# resting stop actually faces, taken on whichever side ran further.
#
# Two things this is not. It is not realised slippage: it ignores book depth
# and the queue, and a thin book will fill worse than the tape suggests. And
# it is not a claim to be conservative in general - it happens to grow in fast
# markets, which is when stops trigger, and that is the direction an error
# here should lean.
STOP_SLIPPAGE_QUANTILE = Decimal("0.95")

# Below this the percentile is the largest of a handful and describes the
# sample. Absent is the answer, and the cost model already knows what to do
# with an input it does not have.
MINIMUM_EXCURSION_BARS = 20


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    entry_fee_per_unit: Decimal | None
    exit_taker_fee_per_unit: Decimal | None

    def __post_init__(self) -> None:
        for name in ("entry_fee_per_unit", "exit_taker_fee_per_unit"):
            value = getattr(self, name)
            if value is None:
                continue
            fee = require_decimal(value)
            if fee < 0:
                raise ValueError(f"{name} must be non-negative")
            object.__setattr__(self, name, fee)


@dataclass(frozen=True, slots=True)
class CostFacts:
    state: EvidenceState
    spread_per_unit: Decimal
    fee_per_unit: Decimal | None
    slippage_allowance_per_unit: Decimal
    raw_cost_per_unit: Decimal | None
    cost_offset_per_unit: Decimal | None
    round_trip_cost: Decimal | None


def estimate_round_trip_cost(
    *,
    spread: Decimal,
    quantity: Decimal,
    fee_schedule: FeeSchedule,
    stop_slippage_q95: Decimal,
    tick_size: Decimal,
) -> CostFacts:
    normalized_spread = _non_negative(spread, "spread")
    normalized_quantity = _non_negative(quantity, "quantity")
    slippage = _non_negative(stop_slippage_q95, "stop_slippage_q95")
    tick = require_decimal(tick_size)
    if tick <= 0:
        raise ValueError("tick_size must be positive")
    if type(fee_schedule) is not FeeSchedule:
        raise TypeError("fee_schedule must be an exact FeeSchedule")
    slippage_allowance = slippage + tick
    if (
        fee_schedule.entry_fee_per_unit is None
        or fee_schedule.exit_taker_fee_per_unit is None
    ):
        return CostFacts(
            state=EvidenceState.UNAVAILABLE,
            spread_per_unit=normalized_spread,
            fee_per_unit=None,
            slippage_allowance_per_unit=slippage_allowance,
            raw_cost_per_unit=None,
            cost_offset_per_unit=None,
            round_trip_cost=None,
        )
    fee = fee_schedule.entry_fee_per_unit + fee_schedule.exit_taker_fee_per_unit
    raw = normalized_spread + fee + slippage_allowance
    offset = (raw / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    return CostFacts(
        state=EvidenceState.AVAILABLE,
        spread_per_unit=normalized_spread,
        fee_per_unit=fee,
        slippage_allowance_per_unit=slippage_allowance,
        raw_cost_per_unit=raw,
        cost_offset_per_unit=offset,
        round_trip_cost=offset * normalized_quantity,
    )


def _non_negative(value: object, name: str) -> Decimal:
    normalized = require_decimal(value)
    if normalized < 0:
        raise ValueError(f"{name} must be non-negative")
    return normalized


__all__ = ("CostFacts", "FeeSchedule", "estimate_round_trip_cost")


def adverse_excursions(bars: Sequence[CompletedOhlcvBar]) -> tuple[Decimal, ...]:
    """Per bar, how far price ran past the previous close, worst side.

    A stop rests at a price the market has to reach. What it is exposed to is
    the distance the market keeps going after reaching it, and within one bar
    that is bounded by how far the bar travelled beyond the last price anyone
    had seen. Both directions are measured and the larger kept: a stop sits on
    one side, but which side is not known when the distribution is built.
    """
    values = tuple(bars)
    if any(type(bar) is not CompletedOhlcvBar for bar in values):
        raise TypeError("adverse excursion requires exact completed bars")
    return tuple(
        max(
            max(Decimal(0), previous.close - current.low),
            max(Decimal(0), current.high - previous.close),
        )
        for previous, current in pairwise(values)
    )


def stop_slippage_from_bars(
    bars: Sequence[CompletedOhlcvBar],
    *,
    quantile: Decimal = STOP_SLIPPAGE_QUANTILE,
    minimum_bars: int = MINIMUM_EXCURSION_BARS,
) -> Decimal | None:
    """The modelled stop slippage, or None when the tape cannot say.

    Nearest rank, so the answer is a distance some bar actually travelled
    rather than one interpolated between two that did not.
    """
    excursions = adverse_excursions(bars)
    if len(excursions) < minimum_bars:
        return None
    ordered = sorted(excursions)
    rank = (quantile * Decimal(len(ordered))).to_integral_value(rounding=ROUND_CEILING)
    index = max(1, min(len(ordered), int(rank)))
    return ordered[index - 1]
