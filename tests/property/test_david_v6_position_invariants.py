from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from autotrader.config.settings import RuntimeMode
from autotrader.domain.enums import Side
from autotrader.operations.david_v6_position import (
    V6ManagedPosition,
    V6PositionActionKind,
    V6PositionFacts,
    manage_v6_position,
)


@given(
    side=st.sampled_from((Side.BUY, Side.SELL)),
    favorable_tenths=st.integers(min_value=7, max_value=100),
)
def test_emitted_position_transitions_never_increase_structural_risk(
    side: Side,
    favorable_tenths: int,
) -> None:
    entry = Decimal("100")
    stop = Decimal("98") if side is Side.BUY else Decimal("102")
    direction = Decimal("1") if side is Side.BUY else Decimal("-1")
    position = V6ManagedPosition(
        side=side,
        initial_entry_price=entry,
        average_entry_price=entry,
        initial_quantity=Decimal("1"),
        remaining_quantity=Decimal("1"),
        initial_stop_price=stop,
        active_stop_price=stop,
        initial_stop_active=True,
        original_approved_risk=Decimal("2"),
        current_worst_case_risk=Decimal("2"),
        add_count=0,
        break_even_active=False,
        fib_25_recorded=False,
        fib_50_recorded=False,
        shadow_1_2r_recorded=False,
        shadow_1_5r_recorded=False,
    )
    price = entry + direction * (Decimal(favorable_tenths) / Decimal("10"))
    facts = V6PositionFacts(
        current_price=price,
        atr_5m=Decimal("2"),
        tick_size=Decimal("0.1"),
        actual_entry_fee_per_unit=Decimal("0.02"),
        taker_exit_fee_per_unit=Decimal("0.03"),
        q95_adverse_stop_slippage=Decimal("0.04"),
        slippage_sample_sufficient=True,
        fib_25_price=entry + direction * Decimal("50"),
        fib_50_price=entry + direction * Decimal("51"),
        fib_66_price=entry + direction * Decimal("52"),
        blocking_big_trade=False,
        metodo_exit_signal=False,
        protection_failed=False,
    )

    actions = manage_v6_position(position, facts, mode=RuntimeMode.PAPER)
    risk = position.current_worst_case_risk
    active_stop = position.active_stop_price
    for action in actions:
        assert action.resulting_worst_case_risk <= risk
        if action.stop_price is not None and active_stop is not None:
            if side is Side.BUY:
                assert action.stop_price >= active_stop
            else:
                assert action.stop_price <= active_stop
            active_stop = action.stop_price
        risk = action.resulting_worst_case_risk
        if action.kind is V6PositionActionKind.ADD_AND_MOVE_STOP:
            assert action.quantity is not None
            assert action.average_entry_price is not None
            assert action.stop_price is not None
            position = replace(
                position,
                average_entry_price=action.average_entry_price,
                remaining_quantity=position.remaining_quantity + action.quantity,
                active_stop_price=action.stop_price,
                add_count=1,
                break_even_active=True,
                current_worst_case_risk=action.resulting_worst_case_risk,
            )

    second_actions = manage_v6_position(position, facts, mode=RuntimeMode.PAPER)
    assert V6PositionActionKind.ADD_AND_MOVE_STOP not in tuple(
        action.kind for action in second_actions
    )
