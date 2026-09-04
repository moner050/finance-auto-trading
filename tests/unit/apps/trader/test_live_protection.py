"""The emergency close's two answers that do not need a database.

Whether the position is actually gone, and what happens when the close it
would send was never prepared. The reader that assembles an `EntryFill` is
query work and belongs with a MySQL that can answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid7

import pytest

from autotrader.apps.trader.live_protection import (
    MySqlEmergencyOrders,
    ProtectionContextUnavailable,
)
from autotrader.domain.enums import Side
from autotrader.execution.orders.models import BrokerOrderCommand
from autotrader.execution.reconciliation.models import BrokerSnapshot, HeldPosition
from autotrader.integrations.brokers.binance_usdm.algo_orders import EntryFill
from autotrader.integrations.brokers.binance_usdm.orders import BrokerWriteResult

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
ACCOUNT = uuid7()
INSTRUMENT = uuid7()
BROKER = uuid7()


def _fill() -> EntryFill:
    return EntryFill(
        entry_command_id=uuid7(),
        account_id=ACCOUNT,
        instrument_id=INSTRUMENT,
        binding_id=uuid7(),
        side=Side.BUY,
        first_fill_quantity=Decimal("0.002"),
        cumulative_quantity_before=Decimal(),
        average_fill_price=Decimal("60000"),
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        filled_at=NOW,
        protection_deadline=NOW + timedelta(seconds=30),
        emergency_close_command_id=uuid7(),
    )


@dataclass
class _Snapshots:
    positions: tuple[HeldPosition, ...] = ()
    complete: bool = True
    asked: int = 0

    async def read_snapshot(
        self, *, account_id: object, now: datetime
    ) -> BrokerSnapshot:
        del account_id
        self.asked += 1
        return BrokerSnapshot(
            broker_id=BROKER,
            account_id=ACCOUNT,
            complete=self.complete,
            expires_at=now + timedelta(seconds=30),
            open_orders=(),
            positions=self.positions,
        )


class _Orders:
    def __init__(self) -> None:
        self.submitted: list[BrokerOrderCommand] = []
        self.recovered: list[str] = []

    async def submit_locked(self, command: BrokerOrderCommand) -> BrokerWriteResult:
        self.submitted.append(command)
        raise AssertionError("not reached in these tests")

    async def recover_by_client_id(self, client_order_id: str) -> BrokerWriteResult:
        self.recovered.append(client_order_id)
        raise AssertionError("not reached in these tests")


def _service(snapshots: _Snapshots) -> MySqlEmergencyOrders:
    return MySqlEmergencyOrders(
        sessions=None,  # pyright: ignore[reportArgumentType]
        orders=_Orders(),  # pyright: ignore[reportArgumentType]
        snapshots=snapshots,  # pyright: ignore[reportArgumentType]
        account_id=ACCOUNT,
        instrument_id=INSTRUMENT,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_an_absent_instrument_is_a_closed_position() -> None:
    """A flat instrument is absent from a snapshot rather than reported at
    zero, which is what makes this readable as absence."""
    snapshots = _Snapshots(positions=())
    assert await _service(snapshots).confirm_zero_position(_fill()) is True
    assert snapshots.asked == 1


@pytest.mark.asyncio
async def test_a_held_instrument_is_not_a_closed_position() -> None:
    snapshots = _Snapshots(
        positions=(HeldPosition(instrument_id=INSTRUMENT, quantity=Decimal("0.002")),)
    )
    assert await _service(snapshots).confirm_zero_position(_fill()) is False


@pytest.mark.asyncio
async def test_another_instrument_being_held_says_nothing_about_this_one() -> None:
    snapshots = _Snapshots(
        positions=(HeldPosition(instrument_id=uuid7(), quantity=Decimal("1")),)
    )
    assert await _service(snapshots).confirm_zero_position(_fill()) is True


@pytest.mark.asyncio
async def test_a_partial_snapshot_cannot_prove_absence() -> None:
    """The instrument may be in the part that did not arrive, and answering
    'closed' from it would stop an emergency close that is still needed."""
    snapshots = _Snapshots(positions=(), complete=False)
    assert await _service(snapshots).confirm_zero_position(_fill()) is False


@pytest.mark.asyncio
async def test_a_close_that_was_never_prepared_says_so() -> None:
    """Not a lookup that went wrong: the position cannot be closed by the
    path that exists for exactly this, and that is what it reports."""

    class _Empty:
        """A session that holds no command, which is the case under test."""

        async def get(self, *_: object) -> None:
            return None

    class _NoSessions:
        def __call__(self) -> _NoSessions:
            return self

        async def __aenter__(self) -> _Empty:
            return _Empty()

        async def __aexit__(self, *_: object) -> None:
            return None

    service = MySqlEmergencyOrders(
        sessions=_NoSessions(),  # pyright: ignore[reportArgumentType]
        orders=_Orders(),  # pyright: ignore[reportArgumentType]
        snapshots=_Snapshots(),  # pyright: ignore[reportArgumentType]
        account_id=ACCOUNT,
        instrument_id=INSTRUMENT,
    )
    with pytest.raises(ProtectionContextUnavailable):
        await service.prepare_full_close(_fill())
