"""Calling the manager that nothing was calling.

`manage_v6_position` was written, tested and unreachable, so a position once
opened was never managed. These are the decisions made in reaching it: which
side it is asked about, what happens to the pass when it acts, and what a mode
that cannot place orders does with an exit it has decided on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from autotrader.apps.trader.position_management import (
    PositionMarket,
    RefusingPositionActions,
    V6PositionManager,
)
from autotrader.config.settings import RuntimeMode
from autotrader.domain.enums import Side
from autotrader.operations.david_v6_position import (
    V6ManagedPosition,
    V6PositionAction,
    V6PositionActionKind,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
POSITION_ID = UUID("01a05fd6-798a-708c-905e-090669986f6a")
ACCOUNT = UUID("01a05ab5-5f49-7252-acb0-4e3ff4a00ce2")
INSTRUMENT = UUID("01a05aa7-debe-73ea-9498-532bd5974b23")


def _held(side: Side = Side.BUY) -> V6ManagedPosition:
    long = side is Side.BUY
    return V6ManagedPosition(
        side=side,
        initial_entry_price=Decimal("100"),
        average_entry_price=Decimal("100"),
        initial_quantity=Decimal("2"),
        remaining_quantity=Decimal("2"),
        initial_stop_price=Decimal("98") if long else Decimal("102"),
        active_stop_price=Decimal("98") if long else Decimal("102"),
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


class _Positions:
    def __init__(self, held: V6ManagedPosition | None) -> None:
        self._held = held
        self.marks_recorded: list[str] = []

    async def open_position(self, *, account_id: UUID, instrument_id: UUID):
        del account_id, instrument_id
        return None if self._held is None else (POSITION_ID, self._held)

    async def marks(self, position_id: UUID) -> frozenset[str]:
        del position_id
        return frozenset()

    async def record_mark(self, *, position_id: UUID, mark: str, now: datetime) -> None:
        del position_id, now
        self.marks_recorded.append(mark)


class _Hlit:
    """Levels, and which side they were asked for."""

    def __init__(self) -> None:
        self.asked: list[Side] = []

    def for_side(self, side: Side) -> None:
        self.asked.append(side)
        return None


class _Sink:
    def __init__(self) -> None:
        self.applied: list[V6PositionAction] = []

    async def apply(
        self,
        action: V6PositionAction,
        *,
        position: V6ManagedPosition,
        position_id: UUID,
        now: datetime,
    ) -> None:
        del position, position_id, now
        self.applied.append(action)


class _Protection:
    def __init__(self, failed: bool = False) -> None:
        self._failed = failed

    async def failed(self, *, account_id: UUID, instrument_id: UUID) -> bool:
        del account_id, instrument_id
        return self._failed


class _Bundle:
    def __init__(self) -> None:
        self.order_flow = _Absent()
        self.metodo = _Absent()
        self.session = _Absent()


class _Absent:
    state = "UNAVAILABLE"
    value = None


class _Fees:
    entry_fee_per_unit = Decimal("0.01")
    exit_taker_fee_per_unit = Decimal("0.02")


def _market(hlit: object = None) -> PositionMarket:
    return PositionMarket(
        bundle=_Bundle(),  # type: ignore[arg-type]
        hlit=hlit,  # type: ignore[arg-type]
        current_price=Decimal("100"),
        atr_5m=Decimal("2"),
        tick_size=Decimal("0.1"),
        fee_schedule=_Fees(),  # type: ignore[arg-type]
        stop_slippage_q95=Decimal("3"),
    )


def _manager(
    positions: _Positions,
    sink: _Sink,
    *,
    protection: _Protection | None = None,
    mode: RuntimeMode = RuntimeMode.SHADOW,
) -> V6PositionManager:
    return V6PositionManager(
        positions=positions,  # type: ignore[arg-type]
        actions=sink,  # type: ignore[arg-type]
        protection=protection or _Protection(),  # type: ignore[arg-type]
        account_id=ACCOUNT,
        instrument_id=INSTRUMENT,
        mode=mode,
    )


@pytest.mark.asyncio
async def test_nothing_held_is_nothing_to_do() -> None:
    sink = _Sink()

    acted = await _manager(_Positions(None), sink).manage(_market(), now=NOW)

    assert acted is False
    assert sink.applied == []


@pytest.mark.asyncio
async def test_the_levels_are_read_for_the_side_that_is_held() -> None:
    """Not the side being evaluated for entry. A short can be held while the
    divergence has already turned long, and reading the fibonacci levels for
    the wrong direction answers a question about a position that is not
    there."""
    hlit = _Hlit()

    await _manager(_Positions(_held(Side.SELL)), _Sink()).manage(_market(hlit), now=NOW)

    assert hlit.asked == [Side.SELL]


@pytest.mark.asyncio
async def test_a_protection_failure_reaches_the_manager() -> None:
    """It is the one fact that makes it exit unconditionally, so a broken
    stop has to arrive rather than be inferred."""
    sink = _Sink()

    acted = await _manager(
        _Positions(_held()), sink, protection=_Protection(failed=True)
    ).manage(_market(), now=NOW)

    assert acted is True
    assert sink.applied[0].kind is V6PositionActionKind.EMERGENCY_EXIT_FULL
    assert sink.applied[0].account_halt is True


@pytest.mark.asyncio
async def test_acting_reports_true_so_the_pass_can_end() -> None:
    """Closing a position and opening another against the same bar would be
    two decisions on one reading of the market."""
    assert (
        await _manager(
            _Positions(_held()), _Sink(), protection=_Protection(failed=True)
        ).manage(_market(), now=NOW)
        is True
    )


@pytest.mark.asyncio
async def test_a_refusing_sink_records_telemetry_and_refuses_orders() -> None:
    """An observation that price reached a level is not an order, so it lands
    even where nothing can be placed. An exit that could not be taken is
    counted, because a session where one was decided and refused is a session
    that would have exited."""
    positions = _Positions(_held())
    sink = RefusingPositionActions(positions)  # type: ignore[arg-type]

    await sink.apply(
        _telemetry_action(), position=_held(), position_id=POSITION_ID, now=NOW
    )
    await sink.apply(_exit_action(), position=_held(), position_id=POSITION_ID, now=NOW)

    assert positions.marks_recorded == [V6PositionActionKind.RECORD_FIB_25.value]
    assert sink.recorded == 1
    assert sink.refused == 1


def _telemetry_action() -> V6PositionAction:
    return V6PositionAction(
        kind=V6PositionActionKind.RECORD_FIB_25,
        reason="FIB_25_REACHED",
        order_style=None,
        quantity=None,
        stop_price=None,
        average_entry_price=Decimal("100"),
        resulting_worst_case_risk=Decimal("4"),
        reduce_only=False,
        telemetry_only=True,
        account_halt=False,
    )


def _exit_action() -> V6PositionAction:
    return V6PositionAction(
        kind=V6PositionActionKind.EXIT_FULL_FIB_66,
        reason="TARGET_REACHED",
        order_style=None,
        quantity=Decimal("2"),
        stop_price=None,
        average_entry_price=Decimal("100"),
        resulting_worst_case_risk=Decimal("0"),
        reduce_only=True,
        telemetry_only=False,
        account_halt=False,
    )
