from __future__ import annotations

from enum import StrEnum
from typing import cast
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.contracts.envelope import EventEnvelope
from autotrader.contracts.execution_events import OrderCreatedPayload
from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.intents.models import IntentOrigin
from autotrader.execution.orders.models import (
    BrokerOrderCommand,
    CommandType,
    Order,
    OrderDomainEvent,
    OrderStatus,
)
from autotrader.execution.orders.service import (
    OrderCommandFactory,
    OrderSubmissionContext,
)
from autotrader.persistence.mysql.models.operations import OpsIncident
from autotrader.persistence.mysql.models.orders import (
    PersistedBrokerOrderStatusEvent,
    PersistedOrder,
    PersistedOrderCommand,
    PersistedOrderCommandAuthority,
    PersistedOrderEvent,
    PersistedOrderStatusWatermark,
)
from autotrader.persistence.mysql.repositories.operations import (
    lock_global_dispatch_guard,
)
from autotrader.persistence.mysql.repositories.outbox import OutboxRepository


class OrderIdentityCollisionError(ValueError):
    pass


class BrokerStatusInsertResult(StrEnum):
    INSERTED = "INSERTED"
    DUPLICATE = "DUPLICATE"
    PAYLOAD_CONFLICT = "PAYLOAD_CONFLICT"


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_order_once(self, order: PersistedOrder) -> PersistedOrder:
        await self._session.execute(
            insert(PersistedOrder)
            .values(
                **{
                    column.name: getattr(order, column.name)
                    for column in PersistedOrder.__table__.columns
                }
            )
            .prefix_with("IGNORE")
        )
        existing = await self._session.scalar(
            select(PersistedOrder)
            .where(PersistedOrder.order_intent_id == order.order_intent_id)
            .with_for_update()
        )
        if existing is None:
            raise RuntimeError("inserted order cannot be read")
        if any(
            getattr(existing, field) != getattr(order, field)
            for field in (
                "risk_decision_id",
                "account_id",
                "instrument_id",
                "broker_client_order_id",
                "side",
                "order_style",
                "requested_quantity",
                "limit_price",
                "trigger_price",
            )
        ):
            raise OrderIdentityCollisionError("order intent identity payload collision")
        return existing

    async def create_command_once(
        self, command: PersistedOrderCommand
    ) -> PersistedOrderCommand:
        await self._session.execute(
            mysql_insert(PersistedOrderCommand)
            .values(
                **{
                    column.name: getattr(command, column.name)
                    for column in PersistedOrderCommand.__table__.columns
                    if column.name != "submit_once_marker"
                }
            )
            .on_duplicate_key_update(id=PersistedOrderCommand.id)
        )
        existing = await self._session.scalar(
            select(PersistedOrderCommand)
            .where(PersistedOrderCommand.idempotency_key == command.idempotency_key)
            .with_for_update()
        )
        if existing is None:
            raise RuntimeError("inserted order command cannot be read")
        if existing.canonical_payload_hash != command.canonical_payload_hash:
            raise OrderIdentityCollisionError(
                "order command identity payload collision"
            )
        return existing

    async def create_event_once(
        self, event: PersistedOrderEvent
    ) -> PersistedOrderEvent:
        await self._session.execute(
            insert(PersistedOrderEvent)
            .values(
                **{
                    column.name: getattr(event, column.name)
                    for column in PersistedOrderEvent.__table__.columns
                }
            )
            .prefix_with("IGNORE")
        )
        existing = await self._session.scalar(
            select(PersistedOrderEvent)
            .where(
                PersistedOrderEvent.order_id == event.order_id,
                PersistedOrderEvent.aggregate_version == event.aggregate_version,
            )
            .with_for_update()
        )
        if existing is None:
            raise RuntimeError("inserted order event cannot be read")
        if (
            existing.status != event.status
            or existing.raw_status != event.raw_status
            or existing.occurred_at != event.occurred_at
        ):
            raise OrderIdentityCollisionError("order event identity payload collision")
        return existing

    async def record_broker_status_once(
        self, event: PersistedBrokerOrderStatusEvent
    ) -> BrokerStatusInsertResult:
        await lock_global_dispatch_guard(self._session)
        result = cast(
            CursorResult[object],
            await self._session.execute(
                insert(PersistedBrokerOrderStatusEvent)
                .values(
                    **{
                        column.name: getattr(event, column.name)
                        for column in PersistedBrokerOrderStatusEvent.__table__.columns
                        if column.name != "id"
                    }
                )
                .prefix_with("IGNORE")
            ),
        )
        existing = await self._session.scalar(
            select(PersistedBrokerOrderStatusEvent)
            .where(
                PersistedBrokerOrderStatusEvent.broker_id == event.broker_id,
                PersistedBrokerOrderStatusEvent.account_id == event.account_id,
                PersistedBrokerOrderStatusEvent.source_partition
                == event.source_partition,
                PersistedBrokerOrderStatusEvent.dedupe_key == event.dedupe_key,
            )
            .with_for_update()
        )
        if existing is None:
            raise RuntimeError("inserted broker status cannot be read")
        if existing.canonical_payload_hash != event.canonical_payload_hash:
            self._session.add(
                OpsIncident(
                    severity="BLOCKING",
                    status="OPEN",
                    reason_code="BROKER_STATUS_IDENTITY_PAYLOAD_CONFLICT",
                    created_at=event.observed_at,
                )
            )
            await self._session.flush()
            return BrokerStatusInsertResult.PAYLOAD_CONFLICT
        return (
            BrokerStatusInsertResult.INSERTED
            if result.rowcount == 1
            else BrokerStatusInsertResult.DUPLICATE
        )

    async def advance_status_watermark(
        self,
        *,
        order_id: UUID,
        source_partition: str,
        source_sequence: int,
    ) -> PersistedOrderStatusWatermark:
        watermark = await self._session.scalar(
            select(PersistedOrderStatusWatermark)
            .where(
                PersistedOrderStatusWatermark.order_id == order_id,
                PersistedOrderStatusWatermark.source_partition == source_partition,
            )
            .with_for_update()
        )
        if watermark is None:
            if source_sequence != 1:
                raise ValueError("cannot advance a non-contiguous status watermark")
            watermark = PersistedOrderStatusWatermark(
                order_id=order_id,
                source_partition=source_partition,
                last_contiguous_sequence=source_sequence,
            )
            self._session.add(watermark)
        elif source_sequence == watermark.last_contiguous_sequence + 1:
            watermark.last_contiguous_sequence = source_sequence
        elif source_sequence != watermark.last_contiguous_sequence:
            raise ValueError("cannot advance a non-contiguous status watermark")
        await self._session.flush()
        return watermark


class MySqlOrderStore:
    """Persists one approved order, its submit command, event, and outbox atomically.

    The caller owns the surrounding SQLAlchemy transaction; every write below uses
    the same session and only flushes, never commits independently.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def command_for_existing_order(
        self,
        *,
        order: PersistedOrder,
        command_type: CommandType,
        submission: OrderSubmissionContext,
        origin: IntentOrigin,
        target_broker_order_id: str,
    ) -> UUID:
        """A further command against an order that already exists.

        `create_approved_once` makes an order and its first command together,
        which is right for a submit and useless for a replace: the order is
        already there and only the command is new. The conversion from row to
        domain order lives here either way, so putting this beside it keeps
        one answer to what a persisted order means.

        The caller owns the transaction and is expected to have amended the
        order's terms already - the command's canonical payload is built from
        them, so a replace that had not changed anything would be a command to
        do nothing.
        """
        canonical = OrderCommandFactory().create(
            order=self._to_domain_order(order),
            command_type=command_type,
            submission=submission,
            origin=origin,
            target_broker_order_id=target_broker_order_id,
        )
        await self._session.execute(
            insert(PersistedOrderCommandAuthority)
            .values(
                order_id=canonical.order_id,
                authority_class=canonical.authority_class,
            )
            .prefix_with("IGNORE")
        )
        stored = await OrderRepository(self._session).create_command_once(
            PersistedOrderCommand(
                id=canonical.id,
                order_id=canonical.order_id,
                account_id=canonical.account_id,
                instrument_id=canonical.instrument_id,
                command_type=canonical.command_type.value,
                command_sequence=canonical.command_sequence,
                target_aggregate_version=canonical.target_aggregate_version,
                idempotency_key=canonical.idempotency_key,
                canonical_payload_hash=canonical.canonical_payload_hash,
                broker_client_order_id=canonical.broker_client_order_id,
                target_broker_order_id=canonical.target_broker_order_id,
                replaces_command_id=canonical.replaces_command_id,
                origin_type=canonical.origin_type,
                authority_class=canonical.authority_class,
                owner_runtime_instance_id=canonical.owner_runtime_instance_id,
                fencing_token=canonical.fencing_token,
                not_after=canonical.not_after,
                side=canonical.side.value,
                order_style=canonical.order_style.value,
                quantity=canonical.quantity,
                limit_price=canonical.limit_price,
                trigger_price=canonical.trigger_price,
                time_in_force=canonical.time_in_force,
                status=canonical.status,
                created_at=submission.created_at,
            )
        )
        return stored.id

    async def create_approved_once(
        self,
        *,
        order: Order,
        command: BrokerOrderCommand,
        event: OrderDomainEvent,
        envelope: EventEnvelope[OrderCreatedPayload],
    ) -> Order:
        repository = OrderRepository(self._session)
        persisted_order = await repository.create_order_once(
            PersistedOrder(
                id=order.id,
                order_intent_id=order.order_intent_id,
                risk_decision_id=order.risk_decision_id,
                account_id=order.account_id,
                instrument_id=order.instrument_id,
                broker_client_order_id=order.broker_client_order_id,
                side=order.side.value,
                order_style=order.order_style.value,
                requested_quantity=order.requested_quantity,
                filled_quantity=0,
                limit_price=order.limit_price,
                trigger_price=order.trigger_price,
                status=order.status.value,
                aggregate_version=order.aggregate_version,
                created_at=order.created_at,
            )
        )
        canonical_order = self._to_domain_order(persisted_order)
        canonical_command = OrderCommandFactory().create(
            order=canonical_order,
            command_type=command.command_type,
            submission=OrderSubmissionContext(
                broker_client_order_id=command.broker_client_order_id,
                owner_runtime_instance_id=command.owner_runtime_instance_id,
                fencing_token=command.fencing_token,
                not_after=command.not_after,
                time_in_force=command.time_in_force,
                authority_class=command.authority_class,
                created_at=canonical_order.created_at,
            ),
            origin=IntentOrigin(command.origin_type),
        )
        await self._session.execute(
            insert(PersistedOrderCommandAuthority)
            .values(
                order_id=canonical_order.id,
                authority_class=canonical_command.authority_class,
            )
            .prefix_with("IGNORE")
        )
        await repository.create_command_once(
            PersistedOrderCommand(
                id=canonical_command.id,
                order_id=canonical_command.order_id,
                account_id=canonical_command.account_id,
                instrument_id=canonical_command.instrument_id,
                command_type=canonical_command.command_type.value,
                command_sequence=canonical_command.command_sequence,
                target_aggregate_version=canonical_command.target_aggregate_version,
                idempotency_key=canonical_command.idempotency_key,
                canonical_payload_hash=canonical_command.canonical_payload_hash,
                broker_client_order_id=canonical_command.broker_client_order_id,
                target_broker_order_id=canonical_command.target_broker_order_id,
                replaces_command_id=canonical_command.replaces_command_id,
                origin_type=canonical_command.origin_type,
                authority_class=canonical_command.authority_class,
                owner_runtime_instance_id=canonical_command.owner_runtime_instance_id,
                fencing_token=canonical_command.fencing_token,
                not_after=canonical_command.not_after,
                side=canonical_command.side.value,
                order_style=canonical_command.order_style.value,
                quantity=canonical_command.quantity,
                limit_price=canonical_command.limit_price,
                trigger_price=canonical_command.trigger_price,
                time_in_force=canonical_command.time_in_force,
                status=canonical_command.status,
                dispatch_attempted_at=None,
                result_state=None,
            )
        )
        persisted_event = await repository.create_event_once(
            PersistedOrderEvent(
                id=envelope.event_id,
                order_id=canonical_order.id,
                aggregate_version=event.aggregate_version,
                status=event.status.value,
                raw_status=event.raw_status,
                occurred_at=event.occurred_at,
            )
        )
        canonical_envelope = envelope.model_copy(
            update={
                "event_id": persisted_event.id,
                "aggregate_id": canonical_order.id,
                "aggregate_version": persisted_event.aggregate_version,
                "trace_id": canonical_order.id.hex,
            }
        )
        await OutboxRepository(self._session).enqueue_once(
            canonical_envelope, next_attempt_at=event.occurred_at
        )
        return canonical_order

    @staticmethod
    def _to_domain_order(order: PersistedOrder) -> Order:
        return Order(
            id=order.id,
            order_intent_id=order.order_intent_id,
            risk_decision_id=order.risk_decision_id,
            account_id=order.account_id,
            instrument_id=order.instrument_id,
            side=Side(order.side),
            order_style=OrderStyle(order.order_style),
            requested_quantity=order.requested_quantity,
            limit_price=order.limit_price,
            trigger_price=order.trigger_price,
            status=OrderStatus(order.status),
            aggregate_version=order.aggregate_version,
            broker_client_order_id=order.broker_client_order_id,
            created_at=order.created_at,
        )
