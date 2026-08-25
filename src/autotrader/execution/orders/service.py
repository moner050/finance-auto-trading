from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from autotrader.contracts.envelope import EventEnvelope
from autotrader.contracts.execution_events import OrderCreatedPayload
from autotrader.domain.enums import OrderStyle
from autotrader.execution.intents.models import IntentOrigin, OrderIntent
from autotrader.execution.orders.models import (
    BrokerOrderCommand,
    CommandType,
    Order,
    OrderDomainEvent,
    OrderStatus,
)
from autotrader.risk.models import RiskDecision, RiskOutcome
from autotrader.shared.ids import new_uuid7


@dataclass(frozen=True, slots=True)
class OrderSubmissionContext:
    broker_client_order_id: str
    owner_runtime_instance_id: UUID | None
    fencing_token: int
    not_after: datetime
    time_in_force: str
    authority_class: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.broker_client_order_id.isascii() or not self.broker_client_order_id:
            raise ValueError("broker_client_order_id must be non-empty ASCII")
        if self.fencing_token <= 0:
            raise ValueError("fencing_token must be positive")
        if self.not_after.tzinfo is None or self.created_at.tzinfo is None:
            raise ValueError("submission timestamps must be timezone-aware")
        if self.not_after <= self.created_at:
            raise ValueError("not_after must be after created_at")
        if not self.time_in_force or not self.authority_class:
            raise ValueError("time_in_force and authority_class are required")


class OrderCommandFactory:
    def create(
        self,
        *,
        order: Order,
        command_type: CommandType,
        submission: OrderSubmissionContext,
        origin: IntentOrigin,
        target_broker_order_id: str | None = None,
    ) -> BrokerOrderCommand:
        allowed_authorities = {
            CommandType.SUBMIT: {
                "SUBMIT_NEW_EXPOSURE",
                "SUBMIT_STRICT_REDUCTION",
            },
            CommandType.CANCEL: {"CANCEL"},
            CommandType.REPLACE: {"REPLACE_NON_INCREASING"},
        }
        if submission.authority_class not in allowed_authorities[command_type]:
            raise ValueError("command authority does not permit its command type")
        target_version = order.aggregate_version
        canonical_payload = {
            "authority_class": submission.authority_class,
            "account_id": str(order.account_id),
            "broker_client_order_id": submission.broker_client_order_id,
            "command_type": command_type.value,
            "fencing_token": submission.fencing_token,
            "limit_price": str(order.limit_price) if order.limit_price else None,
            "not_after": submission.not_after.isoformat(),
            "instrument_id": str(order.instrument_id),
            "order_id": str(order.id),
            "order_style": order.order_style.value,
            "origin_type": origin.value,
            "owner_runtime_instance_id": (
                str(submission.owner_runtime_instance_id)
                if submission.owner_runtime_instance_id is not None
                else None
            ),
            "quantity": str(order.requested_quantity),
            "side": order.side.value,
            "target_broker_order_id": target_broker_order_id,
            "target_aggregate_version": target_version,
            "time_in_force": submission.time_in_force,
        }
        canonical_bytes = json.dumps(
            canonical_payload, sort_keys=True, separators=(",", ":")
        ).encode()
        return BrokerOrderCommand(
            id=new_uuid7(),
            order_id=order.id,
            account_id=order.account_id,
            instrument_id=order.instrument_id,
            command_type=command_type,
            target_aggregate_version=target_version,
            idempotency_key=(f"{order.id.hex}:{command_type.lower()}:{target_version}"),
            command_sequence=target_version,
            canonical_payload_hash=sha256(canonical_bytes).digest(),
            broker_client_order_id=submission.broker_client_order_id,
            target_broker_order_id=target_broker_order_id,
            replaces_command_id=None,
            origin_type=origin.value,
            authority_class=submission.authority_class,
            owner_runtime_instance_id=submission.owner_runtime_instance_id,
            fencing_token=submission.fencing_token,
            not_after=submission.not_after,
            side=order.side,
            order_style=order.order_style,
            quantity=order.requested_quantity,
            limit_price=order.limit_price,
            time_in_force=submission.time_in_force,
        )


class OrderStore(Protocol):
    async def create_approved_once(
        self,
        *,
        order: Order,
        command: BrokerOrderCommand,
        event: OrderDomainEvent,
        envelope: EventEnvelope[OrderCreatedPayload],
    ) -> Order: ...


class OrderService:
    def __init__(self, *, store: OrderStore) -> None:
        self._store = store

    async def create_from_risk_decision(
        self,
        *,
        decision: RiskDecision,
        intent: OrderIntent,
        submission: OrderSubmissionContext,
    ) -> Order | None:
        if decision.outcome is not RiskOutcome.APPROVE:
            return None
        if decision.order_intent_id != intent.id:
            raise ValueError("risk decision must belong to the order intent")
        order = Order(
            id=new_uuid7(),
            order_intent_id=decision.order_intent_id,
            risk_decision_id=decision.id,
            account_id=intent.account_id,
            instrument_id=intent.instrument_id,
            side=intent.side,
            order_style=intent.order_style,
            requested_quantity=decision.approved_quantity,
            limit_price=(
                decision.approved_limit_price
                if intent.order_style is OrderStyle.LIMIT
                else None
            ),
            status=OrderStatus.CREATED,
            aggregate_version=1,
            broker_client_order_id=submission.broker_client_order_id,
            created_at=submission.created_at,
        )
        command = OrderCommandFactory().create(
            order=order,
            command_type=CommandType.SUBMIT,
            submission=submission,
            origin=intent.origin,
        )
        event = OrderDomainEvent(
            order_id=order.id,
            aggregate_version=order.aggregate_version,
            status=OrderStatus.CREATED,
            raw_status="CREATED",
            occurred_at=submission.created_at,
        )
        envelope = EventEnvelope[OrderCreatedPayload](
            event_id=new_uuid7(),
            event_type="execution.order.created",
            schema_version=1,
            occurred_at=submission.created_at,
            observed_at=submission.created_at,
            producer="execution-order-service",
            partition_key=str(order.account_id),
            aggregate_type="Order",
            aggregate_id=order.id,
            aggregate_version=order.aggregate_version,
            correlation_id=order.order_intent_id,
            causation_id=decision.id,
            trace_id=order.id.hex,
            payload=OrderCreatedPayload(
                order_intent_id=str(order.order_intent_id),
                risk_decision_id=str(order.risk_decision_id),
                status=order.status.value,
                requested_quantity=order.requested_quantity,
            ),
        )
        return await self._store.create_approved_once(
            order=order,
            command=command,
            event=event,
            envelope=envelope,
        )
