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
    V6PositionActionKind.EXIT_FULL_SESSION_CLOSE,
    V6PositionActionKind.EMERGENCY_EXIT_FULL,
)
# Everything the manager can decide is now carried out. The lists stay so
# that a kind added to the enum has to be placed in one of them.
NOT_BUILT: tuple[V6PositionActionKind, ...] = ()
STOP_MOVES = (
    V6PositionActionKind.ACTIVATE_INITIAL_STOP,
    V6PositionActionKind.MOVE_STOP_TO_BREAK_EVEN,
)
# Its own category: an add is a stop move and an entry, and refusing it says
# so in its own words.
ADDS = (V6PositionActionKind.ADD_AND_MOVE_STOP,)
TELEMETRY = (
    V6PositionActionKind.RECORD_FIB_25,
    V6PositionActionKind.RECORD_FIB_50_RESEARCH,
    V6PositionActionKind.OBSERVE_PARTIAL_1_2R,
    V6PositionActionKind.OBSERVE_PARTIAL_1_5R,
)


def test_every_action_kind_is_accounted_for() -> None:
    """A kind added to the manager and to neither list here is a kind whose
    handling nobody decided."""
    assert set(BUILT) | set(NOT_BUILT) | set(STOP_MOVES) | set(ADDS) | set(
        TELEMETRY
    ) == set(V6PositionActionKind)


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
    stop_price: Decimal | None = None,
) -> V6PositionAction:
    return V6PositionAction(
        kind=kind,
        reason=kind.value,
        order_style=None,
        quantity=None if telemetry_only else quantity,
        stop_price=stop_price,
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
        quotes=None,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_an_add_without_its_stop_move_is_refused() -> None:
    """Adding without moving the stop enlarges the position behind the stop
    it already had, which is more money at risk than anything approved. The
    move is not an accompaniment to the add; it is the half that pays for it."""
    with pytest.raises(PositionActionUnsupportedError, match="behind the old stop"):
        await _sink().apply(
            _action(V6PositionActionKind.ADD_AND_MOVE_STOP, stop_price=None),
            position=_held(),
            position_id=POSITION_ID,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_an_add_with_no_quantity_is_refused() -> None:
    with pytest.raises(PositionActionUnsupportedError, match="no quantity"):
        await _sink().apply(
            _action(
                V6PositionActionKind.ADD_AND_MOVE_STOP,
                stop_price=Decimal("100"),
                quantity=None,
            ),
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


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", STOP_MOVES)
async def test_a_stop_move_without_a_price_is_refused(
    kind: V6PositionActionKind,
) -> None:
    """The action names where the stop goes. Falling back to the structural
    stop when it does not would move a working stop to a price the manager
    never asked for, and the fallback would look like it worked."""
    with pytest.raises(PositionActionUnsupportedError, match="no stop price"):
        await _sink().apply(
            _action(kind, stop_price=None),
            position=_held(),
            position_id=POSITION_ID,
            now=NOW,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", STOP_MOVES)
async def test_a_stop_move_to_a_non_positive_price_is_refused(
    kind: V6PositionActionKind,
) -> None:
    with pytest.raises(PositionActionUnsupportedError, match="no stop price"):
        await _sink().apply(
            _action(kind, stop_price=Decimal(0)),
            position=_held(),
            position_id=POSITION_ID,
            now=NOW,
        )
