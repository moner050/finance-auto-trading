from __future__ import annotations

from decimal import Decimal

from autotrader.strategies.david_v6.costs import (
    FeeSchedule,
    estimate_round_trip_cost,
)
from autotrader.strategies.david_v6.models import EvidenceState


def test_round_trip_cost_includes_q95_slippage_and_one_extra_tick() -> None:
    facts = estimate_round_trip_cost(
        spread=Decimal("0.21"),
        quantity=Decimal("3"),
        fee_schedule=FeeSchedule(
            entry_fee_per_unit=Decimal("0.03"),
            exit_taker_fee_per_unit=Decimal("0.04"),
        ),
        stop_slippage_q95=Decimal("0.06"),
        tick_size=Decimal("0.05"),
    )

    assert facts.state is EvidenceState.AVAILABLE
    assert facts.raw_cost_per_unit == Decimal("0.39")
    assert facts.cost_offset_per_unit == Decimal("0.40")
    assert facts.round_trip_cost == Decimal("1.20")
    assert facts.slippage_allowance_per_unit == Decimal("0.11")


def test_missing_fee_schedule_is_unavailable_not_zero_cost() -> None:
    facts = estimate_round_trip_cost(
        spread=Decimal("0.1"),
        quantity=Decimal("1"),
        fee_schedule=FeeSchedule(
            entry_fee_per_unit=None,
            exit_taker_fee_per_unit=Decimal("0.02"),
        ),
        stop_slippage_q95=Decimal("0.03"),
        tick_size=Decimal("0.01"),
    )

    assert facts.state is EvidenceState.UNAVAILABLE
    assert facts.cost_offset_per_unit is None
    assert facts.round_trip_cost is None
