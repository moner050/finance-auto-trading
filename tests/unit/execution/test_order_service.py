from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid7

import pytest

from autotrader.domain.enums import IntentType, OrderStyle, Side
from autotrader.execution.intents.models import IntentOrigin, OrderIntent
from autotrader.execution.orders.models import CommandType
from autotrader.execution.orders.service import OrderService, OrderSubmissionContext
from autotrader.risk.models import RiskOutcome


@dataclass(frozen=True, slots=True)
class Decision:
    id: UUID
    order_intent_id: UUID
    outcome: RiskOutcome
    approved_quantity: Decimal = Decimal("1")
    approved_limit_price: Decimal | None = Decimal("10")


class RecordingOrderStore:
    def __init__(self) -> None:
        self.orders: list[object] = []
        self.commands: list[object] = []

    async def create_approved_once(
        self, *, order: object, command: object, event: object, envelope: object
    ) -> object:
        self.orders.append(order)
        self.commands.append(command)
        del event, envelope
        return order


class DeduplicatingOrderStore(RecordingOrderStore):
    async def create_approved_once(
        self, *, order: object, command: object, event: object, envelope: object
    ) -> object:
        for existing in self.orders:
            if existing.order_intent_id == order.order_intent_id:
                return existing
        return await super().create_approved_once(
            order=order, command=command, event=event, envelope=envelope
        )


def intent(*, id: UUID) -> OrderIntent:
    return OrderIntent(
        id=id,
        origin=IntentOrigin.STRATEGY,
        source_id=uuid7(),
        account_id=uuid7(),
        instrument_id=uuid7(),
        intent_type=IntentType.ENTRY,
        side=Side.SELL,
        order_style=OrderStyle.MARKET,
        quantity=Decimal("2"),
        limit_price=None,
        idempotency_key=f"strategy:{uuid7().hex}:{uuid7().hex}",
    )


def submission() -> OrderSubmissionContext:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    return OrderSubmissionContext(
        broker_client_order_id="client-order-id",
        owner_runtime_instance_id=uuid7(),
        fencing_token=1,
        not_after=now + timedelta(minutes=1),
        time_in_force="DAY",
        authority_class="SUBMIT_NEW_EXPOSURE",
        created_at=now,
    )


@pytest.mark.asyncio
async def test_approved_decision_creates_one_order_and_submit_command() -> None:
    store = RecordingOrderStore()
    source_intent = intent(id=uuid7())
    decision = Decision(
        id=uuid7(), order_intent_id=source_intent.id, outcome=RiskOutcome.APPROVE
    )

    order = await OrderService(store=store).create_from_risk_decision(
        decision=decision,
        intent=source_intent,
        submission=submission(),
    )

    assert order is not None
    assert len(store.orders) == 1
    assert len(store.commands) == 1
    assert store.commands[0].command_type is CommandType.SUBMIT
    assert order.side is Side.SELL
    assert order.order_style is OrderStyle.MARKET
    assert order.account_id == source_intent.account_id


@pytest.mark.asyncio
async def test_rejected_decision_creates_no_order_or_command() -> None:
    store = RecordingOrderStore()

    source_intent = intent(id=uuid7())
    order = await OrderService(store=store).create_from_risk_decision(
        decision=Decision(
            id=uuid7(), order_intent_id=source_intent.id, outcome=RiskOutcome.REJECT
        ),
        intent=source_intent,
        submission=submission(),
    )

    assert order is None
    assert store.orders == []
    assert store.commands == []


@pytest.mark.asyncio
async def test_replayed_approved_decision_preserves_one_order_and_submit() -> None:
    store = DeduplicatingOrderStore()
    source_intent = intent(id=uuid7())
    decision = Decision(
        id=uuid7(), order_intent_id=source_intent.id, outcome=RiskOutcome.APPROVE
    )
    service = OrderService(store=store)

    for _ in range(100):
        await service.create_from_risk_decision(
            decision=decision, intent=source_intent, submission=submission()
        )

    assert len(store.orders) == 1
    assert len(store.commands) == 1
