from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid7

import pytest

from autotrader.domain.enums import Side
from autotrader.execution.fills.models import BrokerExecutionEvent, Fill
from autotrader.execution.fills.service import FillApplication, FillService


class Store:
    def __init__(self, *, requested: Decimal, filled: Decimal = Decimal("0")) -> None:
        self.account_id = uuid7()
        self.instrument_id = uuid7()
        self.requested = requested
        self.filled = filled
        self.ids: set[str] = set()
        self.applied: list[tuple[Decimal, bool]] = []

    async def apply_event_once(
        self, value: BrokerExecutionEvent
    ) -> FillApplication | None:
        if value.broker_execution_id in self.ids:
            return None
        self.ids.add(value.broker_execution_id)
        self.filled += value.quantity
        overfill = self.filled > self.requested
        self.applied.append((self.filled, overfill))
        return FillApplication(
            fill=Fill(
                id=uuid7(),
                order_id=value.order_id,
                broker_execution_id=value.broker_execution_id,
                quantity=value.quantity,
                price=value.price,
                side=value.side,
                executed_at=value.executed_at,
                charges=value.charges,
            ),
            total_filled_quantity=self.filled,
            overfill=overfill,
        )


def event(
    store: Store, *, execution_id: str, quantity: Decimal
) -> BrokerExecutionEvent:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    return BrokerExecutionEvent(
        id=uuid7(),
        broker_id=uuid7(),
        account_id=store.account_id,
        order_id=uuid7(),
        broker_order_id="broker",
        broker_client_order_id="client",
        broker_execution_id=execution_id,
        source_partition="fills",
        source_sequence=1,
        instrument_id=store.instrument_id,
        side=Side.BUY,
        quantity=quantity,
        price=Decimal("10"),
        charges=(),
        currency="USD",
        executed_at=now,
        observed_at=now,
        payload_hash=b"x" * 32,
    )


@pytest.mark.asyncio
async def test_duplicate_execution_is_a_noop_and_overfill_is_preserved() -> None:
    store = Store(requested=Decimal("2"))
    service = FillService(store=store)

    first = await service.ingest(
        event(store, execution_id="one", quantity=Decimal("2"))
    )
    duplicate = await service.ingest(
        event(store, execution_id="one", quantity=Decimal("2"))
    )
    overfill = await service.ingest(
        event(store, execution_id="two", quantity=Decimal("3"))
    )

    assert first is not None and first.overfill is False
    assert duplicate is None
    assert overfill is not None and overfill.overfill is True
    assert store.applied == [(Decimal("2"), False), (Decimal("5"), True)]
