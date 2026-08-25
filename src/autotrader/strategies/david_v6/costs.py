from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from autotrader.shared.decimal import require_decimal
from autotrader.strategies.david_v6.models import EvidenceState


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
