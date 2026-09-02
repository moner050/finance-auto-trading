from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autotrader.config.settings import RuntimeMode
from autotrader.domain.enums import OrderStyle, Side
from autotrader.operations.david_v6_position import (
    V6ManagedPosition,
    V6PositionAction,
    V6PositionActionKind,
    V6PositionFacts,
    manage_v6_position,
)


def _position(**changes: object) -> V6ManagedPosition:
    position = V6ManagedPosition(
        side=Side.BUY,
        initial_entry_price=Decimal("100"),
        average_entry_price=Decimal("100"),
        initial_quantity=Decimal("1"),
        remaining_quantity=Decimal("1"),
        initial_stop_price=Decimal("98"),
        active_stop_price=Decimal("98"),
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
    return replace(position, **changes)


def _facts(**changes: object) -> V6PositionFacts:
    facts = V6PositionFacts(
        current_price=Decimal("100"),
        atr_5m=Decimal("2"),
        tick_size=Decimal("0.1"),
        actual_entry_fee_per_unit=Decimal("0.02"),
        taker_exit_fee_per_unit=Decimal("0.03"),
        q95_adverse_stop_slippage=Decimal("0.04"),
        slippage_sample_sufficient=True,
        fib_25_price=Decimal("101"),
        fib_50_price=Decimal("102"),
        fib_66_price=Decimal("103"),
        blocking_big_trade=False,
        metodo_exit_signal=False,
        protection_failed=False,
    )
    return replace(facts, **changes)


def _kinds(actions: tuple[object, ...]) -> tuple[V6PositionActionKind, ...]:
    return tuple(action.kind for action in actions)  # type: ignore[attr-defined]


def test_initial_protection_is_activated_before_other_management() -> None:
    actions = manage_v6_position(
        _position(initial_stop_active=False, active_stop_price=None),
        _facts(current_price=Decimal("101")),
        mode=RuntimeMode.PAPER,
    )

    assert _kinds(actions) == (V6PositionActionKind.ACTIVATE_INITIAL_STOP,)
    assert actions[0].stop_price == Decimal("98")


def test_one_equal_size_add_moves_stop_to_weighted_break_even_with_costs() -> None:
    actions = manage_v6_position(
        _position(),
        _facts(current_price=Decimal("101")),
        mode=RuntimeMode.PAPER,
    )

    assert _kinds(actions) == (V6PositionActionKind.ADD_AND_MOVE_STOP,)
    assert actions[0].quantity == Decimal("1")
    assert actions[0].stop_price == Decimal("100.7")
    assert actions[0].resulting_worst_case_risk == Decimal("0")


@pytest.mark.parametrize(
    ("position", "price"),
    (
        (_position(add_count=1), Decimal("101")),
        (_position(), Decimal("100.69")),
        (_position(), Decimal("99")),
    ),
)
def test_add_is_not_emitted_twice_or_without_favorable_threshold(
    position: V6ManagedPosition,
    price: Decimal,
) -> None:
    actions = manage_v6_position(
        position,
        _facts(current_price=price),
        mode=RuntimeMode.PAPER,
    )

    assert V6PositionActionKind.ADD_AND_MOVE_STOP not in _kinds(actions)


def test_add_and_break_even_require_sufficient_slippage_samples() -> None:
    actions = manage_v6_position(
        _position(),
        _facts(
            current_price=Decimal("101"),
            slippage_sample_sufficient=False,
        ),
        mode=RuntimeMode.PAPER,
    )

    assert V6PositionActionKind.ADD_AND_MOVE_STOP not in _kinds(actions)
    assert V6PositionActionKind.MOVE_STOP_TO_BREAK_EVEN not in _kinds(actions)


def test_general_break_even_starts_at_exactly_point_three_r() -> None:
    actions = manage_v6_position(
        _position(add_count=1),
        _facts(current_price=Decimal("100.6")),
        mode=RuntimeMode.PAPER,
    )

    assert _kinds(actions) == (V6PositionActionKind.MOVE_STOP_TO_BREAK_EVEN,)
    assert actions[0].stop_price == Decimal("100.2")


def test_stop_is_never_widened() -> None:
    actions = manage_v6_position(
        _position(active_stop_price=Decimal("101")),
        _facts(current_price=Decimal("101")),
        mode=RuntimeMode.PAPER,
    )

    assert _kinds(actions) == (V6PositionActionKind.ADD_AND_MOVE_STOP,)
    assert actions[0].stop_price == Decimal("101")


def test_fibonacci_25_and_50_emit_observation_only() -> None:
    actions = manage_v6_position(
        _position(add_count=1, break_even_active=True),
        _facts(current_price=Decimal("102")),
        mode=RuntimeMode.PAPER,
    )

    assert _kinds(actions) == (
        V6PositionActionKind.RECORD_FIB_25,
        V6PositionActionKind.RECORD_FIB_50_RESEARCH,
    )
    assert all(action.telemetry_only for action in actions)
    assert all(action.quantity is None for action in actions)


def test_fibonacci_66_full_close_dominates_all_other_actions() -> None:
    actions = manage_v6_position(
        _position(),
        _facts(current_price=Decimal("103")),
        mode=RuntimeMode.SHADOW,
    )

    assert _kinds(actions) == (V6PositionActionKind.EXIT_FULL_FIB_66,)
    assert actions[0].quantity == Decimal("1")
    assert actions[0].reduce_only is True


@pytest.mark.parametrize(
    ("mode", "expected"),
    (
        (
            RuntimeMode.SHADOW,
            (
                V6PositionActionKind.OBSERVE_PARTIAL_1_2R,
                V6PositionActionKind.OBSERVE_PARTIAL_1_5R,
            ),
        ),
        (RuntimeMode.PAPER, ()),
        (RuntimeMode.LIVE, ()),
    ),
)
def test_r_partials_are_shadow_telemetry_only(
    mode: RuntimeMode,
    expected: tuple[V6PositionActionKind, ...],
) -> None:
    actions = manage_v6_position(
        _position(
            add_count=1,
            break_even_active=True,
            fib_25_recorded=True,
            fib_50_recorded=True,
        ),
        _facts(
            current_price=Decimal("103"),
            fib_66_price=Decimal("110"),
        ),
        mode=mode,
    )

    assert _kinds(actions) == expected
    assert all(action.telemetry_only for action in actions)
    assert all(action.quantity is None for action in actions)


@pytest.mark.parametrize(
    ("fact_changes", "kind", "halt"),
    (
        (
            {"blocking_big_trade": True},
            V6PositionActionKind.EXIT_FULL_BLOCKING_BIG_TRADE,
            False,
        ),
        (
            {"protection_failed": True},
            V6PositionActionKind.EMERGENCY_EXIT_FULL,
            True,
        ),
    ),
)
def test_safety_exit_is_full_reduce_only_and_dominates(
    fact_changes: dict[str, object],
    kind: V6PositionActionKind,
    halt: bool,
) -> None:
    actions = manage_v6_position(
        _position(),
        _facts(current_price=Decimal("103"), **fact_changes),
        mode=RuntimeMode.LIVE,
    )

    assert _kinds(actions) == (kind,)
    assert actions[0].quantity == Decimal("1")
    assert actions[0].reduce_only is True
    assert actions[0].account_halt is halt


def test_short_add_uses_inverse_favorable_and_break_even_directions() -> None:
    position = _position(
        side=Side.SELL,
        initial_stop_price=Decimal("102"),
        active_stop_price=Decimal("102"),
    )

    actions = manage_v6_position(
        position,
        _facts(
            current_price=Decimal("99"),
            fib_25_price=Decimal("99"),
            fib_50_price=Decimal("98"),
            fib_66_price=Decimal("97"),
        ),
        mode=RuntimeMode.PAPER,
    )

    assert _kinds(actions) == (V6PositionActionKind.ADD_AND_MOVE_STOP,)
    assert actions[0].stop_price == Decimal("99.3")


def test_metodo_cross_down_exits_the_whole_position() -> None:
    """Section 12 exit rule: cross_down(sma6, sma70) closes the swing."""
    actions = manage_v6_position(
        _position(),
        _facts(metodo_exit_signal=True),
        mode=RuntimeMode.PAPER,
    )

    assert len(actions) == 1
    assert actions[0].kind is V6PositionActionKind.EXIT_FULL_METODO_CROSS_DOWN
    assert actions[0].reduce_only is True
    assert actions[0].account_halt is False


def test_a_metodo_position_needs_no_hlit_levels() -> None:
    actions = manage_v6_position(
        _position(),
        _facts(fib_25_price=None, fib_50_price=None, fib_66_price=None),
        mode=RuntimeMode.PAPER,
    )

    assert all(
        action.kind
        not in {
            V6PositionActionKind.EXIT_FULL_FIB_66,
            V6PositionActionKind.RECORD_FIB_25,
            V6PositionActionKind.RECORD_FIB_50_RESEARCH,
        }
        for action in actions
    )


def test_protection_failure_still_outranks_the_metodo_exit() -> None:
    actions = manage_v6_position(
        _position(),
        _facts(metodo_exit_signal=True, protection_failed=True),
        mode=RuntimeMode.PAPER,
    )

    assert actions[0].kind is V6PositionActionKind.EMERGENCY_EXIT_FULL
    assert actions[0].account_halt is True


def test_every_full_exit_leaves_at_market() -> None:
    """Section 9.3 concedes price on the way out instead of resting a limit."""
    exits = (
        _facts(protection_failed=True),
        _facts(blocking_big_trade=True),
        _facts(metodo_exit_signal=True),
        _facts(current_price=Decimal("103")),
    )

    for facts in exits:
        actions = manage_v6_position(_position(), facts, mode=RuntimeMode.PAPER)
        assert actions[0].order_style is OrderStyle.MARKET


def test_stop_placement_actions_carry_no_entry_order_style() -> None:
    actions = manage_v6_position(
        _position(active_stop_price=None, initial_stop_active=False),
        _facts(),
        mode=RuntimeMode.PAPER,
    )

    assert actions[0].kind is V6PositionActionKind.ACTIVATE_INITIAL_STOP
    assert actions[0].order_style is None


def test_a_telemetry_action_cannot_carry_an_order() -> None:
    """Section 15.2 puts several items at `telemetry_only` - recorded, never
    allowed to move a decision - and section 11.4 gives the 25% and 50%
    retracements no orders at all. The flag said so and nothing read it: every
    telemetry action happened to be built with no quantity, so the rule held
    by construction and would have stopped holding the first time one was
    built any other way."""
    with pytest.raises(ValueError, match="cannot carry an order"):
        V6PositionAction(
            kind=V6PositionActionKind.RECORD_FIB_25,
            reason="RECORD_FIB_25",
            order_style=OrderStyle.MARKET,
            quantity=Decimal("1"),
            stop_price=None,
            average_entry_price=None,
            resulting_worst_case_risk=Decimal("0"),
            reduce_only=False,
            telemetry_only=True,
            account_halt=False,
        )


def test_the_fibonacci_observations_place_no_orders() -> None:
    """25% is the first reaction and 50% is balance and management. Neither is
    an exit, and 66% is the only one that is."""
    observations = (
        V6PositionActionKind.RECORD_FIB_25,
        V6PositionActionKind.RECORD_FIB_50_RESEARCH,
    )

    for kind in observations:
        action = V6PositionAction(
            kind=kind,
            reason=kind.value,
            order_style=None,
            quantity=None,
            stop_price=None,
            average_entry_price=None,
            resulting_worst_case_risk=Decimal("0"),
            reduce_only=False,
            telemetry_only=True,
            account_halt=False,
        )
        assert action.quantity is None
        assert action.order_style is None


def test_the_session_deadline_closes_the_position() -> None:
    """Section 7 wants a flat book before the close and forbids overnight.
    Until this, `must_be_flat` was computed and read only as a blocker on a
    new entry, which does nothing about what is already held - the position
    stayed open through the night, and through the thin liquidity the session
    boundary was measured to avoid."""
    actions = manage_v6_position(
        _position(), _facts(must_be_flat=True), mode=RuntimeMode.PAPER
    )

    assert _kinds(actions) == (V6PositionActionKind.EXIT_FULL_SESSION_CLOSE,)


def test_a_broken_stop_outranks_the_session_deadline() -> None:
    """A deadline cannot be argued with, but a position with no working
    protection is more urgent still - and only the emergency halts the
    account."""
    actions = manage_v6_position(
        _position(),
        _facts(must_be_flat=True, protection_failed=True),
        mode=RuntimeMode.PAPER,
    )

    assert _kinds(actions) == (V6PositionActionKind.EMERGENCY_EXIT_FULL,)


def test_the_session_deadline_outranks_the_market_reads() -> None:
    """The tape can say whatever it likes; the position is out either way, and
    the recorded reason should be the one that was not negotiable."""
    actions = manage_v6_position(
        _position(),
        _facts(must_be_flat=True, blocking_big_trade=True, metodo_exit_signal=True),
        mode=RuntimeMode.PAPER,
    )

    assert _kinds(actions) == (V6PositionActionKind.EXIT_FULL_SESSION_CLOSE,)


def test_an_open_session_leaves_the_position_alone() -> None:
    actions = manage_v6_position(
        _position(), _facts(must_be_flat=False), mode=RuntimeMode.PAPER
    )

    assert V6PositionActionKind.EXIT_FULL_SESSION_CLOSE not in _kinds(actions)
