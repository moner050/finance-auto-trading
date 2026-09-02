"""What the order sink will and will not do.

Half of `manage_v6_position`'s actions are built here and half are not, and
the line between them is the point of these tests. An action decided and then
quietly dropped is the exact failure this whole path exists to fix, so the
ones that are not built have to stop the run and name themselves rather than
pass silently.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from autotrader.apps.trader.composition import (
    MySqlPositionActions,
    PositionActionUnsupportedError,
)
from autotrader.domain.enums import Side
from autotrader.operations.david_v6_position import (
    V6ManagedPosition,
    V6PositionAction,
    V6PositionActionKind,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
POSITION_ID = UUID("01a05fd6-798a-708c-905e-090669986f6a")
INSTRUMENT = UUID("01a05aa7-debe-73ea-9498-532bd5974b23")

# Every kind the manager can decide, split by whether an order can be made of
# it today. Written out rather than derived so adding a kind to the enum
# without deciding which side it falls on fails here.
BUILT = (
    V6PositionActionKind.EXIT_FULL_FIB_66,
    V6PositionActionKind.EXIT_FULL_METODO_CROSS_DOWN,
    V6PositionActionKind.EXIT_FULL_BLOCKING_BIG_TRADE,
    V6PositionActionKind.EMERGENCY_EXIT_FULL,
)
NOT_BUILT = (
    V6PositionActionKind.ACTIVATE_INITIAL_STOP,
    V6PositionActionKind.ADD_AND_MOVE_STOP,
    V6PositionActionKind.MOVE_STOP_TO_BREAK_EVEN,
)
TELEMETRY = (
    V6PositionActionKind.RECORD_FIB_25,
    V6PositionActionKind.RECORD_FIB_50_RESEARCH,
    V6PositionActionKind.OBSERVE_PARTIAL_1_2R,
    V6PositionActionKind.OBSERVE_PARTIAL_1_5R,
)


def test_every_action_kind_is_accounted_for() -> None:
    """A kind added to the manager and to neither list here is a kind whose
    handling nobody decided."""
    assert set(BUILT) | set(NOT_BUILT) | set(TELEMETRY) == set(V6PositionActionKind)


def _held() -> V6ManagedPosition:
    return V6ManagedPosition(
        side=Side.BUY,
        initial_entry_price=Decimal("100"),
        average_entry_price=Decimal("100"),
        initial_quantity=Decimal("2"),
        remaining_quantity=Decimal("2"),
        initial_stop_price=Decimal("98"),
        active_stop_price=Decimal("98"),
        initial_stop_active=True,
        original_approved_risk=Decimal("4"),
        current_worst_case_risk=Decimal("4"),
        add_count=0,
        break_even_active=False,
        fib_25_recorded=False,
        fib_50_recorded=False,
        shadow_1_2r_recorded=False,
        shadow_1_5r_recorded=False,
    )


def _action(
    kind: V6PositionActionKind,
    *,
    telemetry_only: bool = False,
    halt: bool = False,
    quantity: Decimal | None = Decimal("2"),
) -> V6PositionAction:
    return V6PositionAction(
        kind=kind,
        reason=kind.value,
        order_style=None,
        quantity=None if telemetry_only else quantity,
        stop_price=None,
        average_entry_price=Decimal("100"),
        resulting_worst_case_risk=Decimal("0"),
        reduce_only=not telemetry_only,
        telemetry_only=telemetry_only,
        account_halt=halt,
    )


def _sink() -> MySqlPositionActions:
    return MySqlPositionActions(
        sessions=None,  # type: ignore[arg-type]
        account=None,  # type: ignore[arg-type]
        instrument_id=INSTRUMENT,
        broker=None,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", NOT_BUILT)
async def test_an_action_this_sink_cannot_carry_out_stops_the_run(
    kind: V6PositionActionKind,
) -> None:
    """Moving a stop means cancelling the working one first, and adding
    increases exposure and so needs a real reservation. Neither happened
    before this path existed either, so refusing changes nothing about how a
    position behaves - doing them half-right would change how much is at
    risk."""
    with pytest.raises(PositionActionUnsupportedError, match=kind.value):
        await _sink().apply(
            _action(kind),
            position=_held(),
            position_id=POSITION_ID,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_a_closing_action_with_nothing_left_to_close_does_nothing() -> None:
    """Reached with no session and no broker, which is the assertion: a zero
    quantity must not get as far as opening a transaction."""
    await _sink().apply(
        _action(V6PositionActionKind.EXIT_FULL_FIB_66, quantity=Decimal(0)),
        position=_held(),
        position_id=POSITION_ID,
        now=NOW,
    )
