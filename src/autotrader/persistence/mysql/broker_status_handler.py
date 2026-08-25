from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.contracts.envelope import EventEnvelope
from autotrader.contracts.execution_events import OrderStatusAppliedPayload
from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import (
    BrokerOrderLinkState,
    BrokerOrderStatusEvent,
    BrokerStatusWatermark,
    DeferredBrokerStatus,
    Order,
    OrderStatus,
)
from autotrader.execution.orders.state_machine import (
    InvalidOrderTransitionError,
    OrderStateMachine,
)
from autotrader.persistence.mysql.models.operations import OpsAuditLog, OpsIncident
from autotrader.persistence.mysql.models.orders import (
    PersistedBrokerOrderLink,
    PersistedBrokerOrderStatusEvent,
    PersistedOrder,
    PersistedOrderEvent,
    PersistedOrderStatusWatermark,
)
from autotrader.persistence.mysql.repositories.operations import (
    lock_global_dispatch_guard,
)
from autotrader.persistence.mysql.repositories.orders import (
    BrokerStatusInsertResult,
    OrderRepository,
)
from autotrader.persistence.mysql.repositories.outbox import OutboxRepository
from autotrader.shared.ids import new_uuid7


@dataclass(frozen=True, slots=True)
class BrokerStatusApplication:
    applied: bool
    terminal_release_pending: bool


class MySqlBrokerStatusStore:
    """Applies persisted broker-status evidence within the inbox transaction.

    Status evidence is retained before any state mutation. Missing execution
    completeness never releases a reservation here: it is recorded as a durable
    audit condition for the checkpoint/reconciliation path to re-evaluate.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._orders = OrderRepository(session)

    async def apply_event_once(
        self, event: BrokerOrderStatusEvent
    ) -> BrokerStatusApplication:
        if (
            event.order_id is None
            or event.broker_order_id is None
            or event.broker_client_order_id is None
            or event.requested_quantity is None
            or event.cumulative_filled_quantity is None
            or event.observed_at is None
        ):
            raise ValueError("resolved broker status evidence is required")
        await lock_global_dispatch_guard(self._session)
        result = await self._orders.record_broker_status_once(
            PersistedBrokerOrderStatusEvent(
                id=new_uuid7(),
                order_id=event.order_id,
                broker_id=event.broker_id,
                account_id=event.account_id,
                source_partition=event.source_partition,
                dedupe_key=event.dedupe_key,
                canonical_payload_hash=event.payload_hash,
                broker_order_id=event.broker_order_id,
                broker_client_order_id=event.broker_client_order_id,
                raw_status=event.raw_status,
                requested_quantity=event.requested_quantity,
                cumulative_filled_quantity=event.cumulative_filled_quantity,
                source_sequence=event.source_sequence,
                occurred_at=event.occurred_at,
                observed_at=event.observed_at,
            )
        )
        if result is not BrokerStatusInsertResult.INSERTED:
            return BrokerStatusApplication(
                applied=False, terminal_release_pending=False
            )
        if event.source_sequence is None:
            persisted = await self._session.scalar(
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
            if persisted is None:
                raise RuntimeError("persisted broker status cannot be read")
            return await self._apply_one(persisted)
        return await self._apply_contiguous(
            order_id=event.order_id,
            source_partition=event.source_partition,
        )

    async def _apply_contiguous(
        self,
        *,
        order_id: UUID,
        source_partition: str,
    ) -> BrokerStatusApplication:
        applied = False
        pending = False
        while True:
            watermark = await self._session.scalar(
                select(PersistedOrderStatusWatermark)
                .where(
                    PersistedOrderStatusWatermark.order_id == order_id,
                    PersistedOrderStatusWatermark.source_partition == source_partition,
                )
                .with_for_update()
            )
            expected = (
                1 if watermark is None else watermark.last_contiguous_sequence + 1
            )
            persisted = await self._session.scalar(
                select(PersistedBrokerOrderStatusEvent)
                .where(
                    PersistedBrokerOrderStatusEvent.order_id == order_id,
                    PersistedBrokerOrderStatusEvent.source_partition
                    == source_partition,
                    PersistedBrokerOrderStatusEvent.source_sequence == expected,
                )
                .limit(1)
                .with_for_update()
            )
            if persisted is None:
                return BrokerStatusApplication(
                    applied=applied, terminal_release_pending=pending
                )
            outcome = await self._apply_one(persisted)
            applied = applied or outcome.applied
            pending = pending or outcome.terminal_release_pending
            if not outcome.applied:
                return BrokerStatusApplication(
                    applied=applied, terminal_release_pending=pending
                )

    async def _apply_one(
        self, persisted: PersistedBrokerOrderStatusEvent
    ) -> BrokerStatusApplication:
        if persisted.order_id is None:
            raise ValueError("broker status is missing canonical order")
        order = await self._session.scalar(
            select(PersistedOrder)
            .where(PersistedOrder.id == persisted.order_id)
            .with_for_update()
        )
        if order is None:
            raise LookupError("canonical order not found")
        if order.account_id != persisted.account_id:
            raise ValueError("broker status account does not match canonical order")
        links = tuple(
            BrokerOrderLinkState(
                id=link.id,
                broker_order_id=link.broker_order_id,
                link_sequence=link.link_sequence,
                exposure_bearing=link.exposure_bearing,
                status=OrderStatus(link.status),
            )
            for link in list(
                (
                    await self._session.scalars(
                        select(PersistedBrokerOrderLink)
                        .where(PersistedBrokerOrderLink.order_id == order.id)
                        .order_by(
                            PersistedBrokerOrderLink.link_sequence,
                            PersistedBrokerOrderLink.id,
                        )
                        .with_for_update()
                    )
                ).all()
            )
        )
        if not any(link.broker_order_id == persisted.broker_order_id for link in links):
            raise ValueError("broker status is not linked to the canonical order")
        watermark = await self._session.scalar(
            select(PersistedOrderStatusWatermark)
            .where(
                PersistedOrderStatusWatermark.order_id == order.id,
                PersistedOrderStatusWatermark.source_partition
                == persisted.source_partition,
            )
            .with_for_update()
        )
        try:
            transition = OrderStateMachine().apply(
                _to_domain_order(order),
                _to_domain_status_event(persisted, order_id=order.id),
                links=links,
                watermarks=(
                    BrokerStatusWatermark(
                        source_partition=watermark.source_partition,
                        last_contiguous_sequence=watermark.last_contiguous_sequence,
                    ),
                )
                if watermark is not None
                else (),
            )
        except InvalidOrderTransitionError:
            if persisted.source_sequence is not None:
                raise
            return await self._record_unsequenced_ordering_ambiguity(
                order=order,
                persisted=persisted,
            )
        if isinstance(transition, DeferredBrokerStatus):
            if transition.reason in {"LIVE_SUCCESSOR_LINK", "STALE_STATUS"}:
                matching_link = await self._session.scalar(
                    select(PersistedBrokerOrderLink)
                    .where(
                        PersistedBrokerOrderLink.order_id == order.id,
                        PersistedBrokerOrderLink.broker_order_id
                        == persisted.broker_order_id,
                    )
                    .with_for_update()
                )
                if matching_link is None:
                    raise RuntimeError("locked broker link cannot be read")
                if transition.reason == "LIVE_SUCCESSOR_LINK":
                    matching_link.status = persisted.raw_status
                if persisted.source_sequence is not None:
                    await self._orders.advance_status_watermark(
                        order_id=order.id,
                        source_partition=persisted.source_partition,
                        source_sequence=persisted.source_sequence,
                    )
                await self._session.flush()
                return BrokerStatusApplication(
                    applied=True, terminal_release_pending=False
                )
            return BrokerStatusApplication(
                applied=False, terminal_release_pending=False
            )
        order.status = transition.order.status.value
        order.aggregate_version = transition.order.aggregate_version
        matching_link = await self._session.scalar(
            select(PersistedBrokerOrderLink)
            .where(
                PersistedBrokerOrderLink.order_id == order.id,
                PersistedBrokerOrderLink.broker_order_id == persisted.broker_order_id,
            )
            .with_for_update()
        )
        if matching_link is None:
            raise RuntimeError("locked broker link cannot be read")
        matching_link.status = transition.order.status.value
        if transition.watermark is not None:
            await self._orders.advance_status_watermark(
                order_id=order.id,
                source_partition=transition.watermark.source_partition,
                source_sequence=transition.watermark.last_contiguous_sequence,
            )
        self._session.add(
            PersistedOrderEvent(
                id=new_uuid7(),
                order_id=order.id,
                aggregate_version=order.aggregate_version,
                status=order.status,
                raw_status=persisted.raw_status,
                occurred_at=persisted.occurred_at,
            )
        )
        exposure_link_statuses = list(
            (
                await self._session.scalars(
                    select(PersistedBrokerOrderLink.status).where(
                        PersistedBrokerOrderLink.order_id == order.id,
                        PersistedBrokerOrderLink.exposure_bearing.is_(True),
                    )
                )
            ).all()
        )
        links_terminal = bool(exposure_link_statuses) and all(
            status
            in {
                OrderStatus.FILLED.value,
                OrderStatus.CANCELED.value,
                OrderStatus.REJECTED.value,
                OrderStatus.EXPIRED.value,
            }
            for status in exposure_link_statuses
        )
        terminal_release_pending = links_terminal
        if terminal_release_pending:
            self._session.add(
                OpsAuditLog(
                    action="TERMINAL_RELEASE_PENDING",
                    scope_type="ORDER",
                    scope_key=str(order.id),
                    actor_runtime_instance_id=None,
                    fencing_token=0,
                    details={"reason": "EXECUTION_COMPLETENESS_REQUIRED"},
                    occurred_at=persisted.observed_at,
                )
            )
        await OutboxRepository(self._session).enqueue_once(
            EventEnvelope[OrderStatusAppliedPayload](
                event_id=new_uuid7(),
                event_type="execution.order.status-applied",
                schema_version=1,
                occurred_at=persisted.occurred_at,
                observed_at=persisted.observed_at,
                producer="execution-broker-status-handler",
                partition_key=str(order.account_id),
                aggregate_type="Order",
                aggregate_id=order.id,
                aggregate_version=order.aggregate_version,
                correlation_id=order.order_intent_id,
                causation_id=persisted.id,
                trace_id=order.id.hex,
                payload=OrderStatusAppliedPayload(
                    broker_order_id=persisted.broker_order_id,
                    raw_status=persisted.raw_status,
                    status=order.status,
                    terminal_release_pending=terminal_release_pending,
                ),
            ),
            next_attempt_at=persisted.observed_at,
        )
        await self._session.flush()
        return BrokerStatusApplication(
            applied=True, terminal_release_pending=terminal_release_pending
        )

    async def _record_unsequenced_ordering_ambiguity(
        self,
        *,
        order: PersistedOrder,
        persisted: PersistedBrokerOrderStatusEvent,
    ) -> BrokerStatusApplication:
        matching_link = await self._session.scalar(
            select(PersistedBrokerOrderLink)
            .where(
                PersistedBrokerOrderLink.order_id == order.id,
                PersistedBrokerOrderLink.broker_order_id == persisted.broker_order_id,
            )
            .with_for_update()
        )
        if matching_link is None:
            raise RuntimeError("locked broker link cannot be read")
        matching_link.status = OrderStatus.UNKNOWN.value
        if order.status != OrderStatus.UNKNOWN.value:
            order.status = OrderStatus.UNKNOWN.value
            order.aggregate_version += 1
            status_event = PersistedOrderEvent(
                id=new_uuid7(),
                order_id=order.id,
                aggregate_version=order.aggregate_version,
                status=order.status,
                raw_status="UNSEQUENCED_STATUS_ORDERING_AMBIGUITY",
                occurred_at=persisted.occurred_at,
            )
            self._session.add(status_event)
            await OutboxRepository(self._session).enqueue_once(
                EventEnvelope[OrderStatusAppliedPayload](
                    event_id=status_event.id,
                    event_type="execution.order.status-applied",
                    schema_version=1,
                    occurred_at=persisted.occurred_at,
                    observed_at=persisted.observed_at,
                    producer="execution-broker-status-handler",
                    partition_key=str(order.account_id),
                    aggregate_type="Order",
                    aggregate_id=order.id,
                    aggregate_version=order.aggregate_version,
                    correlation_id=order.order_intent_id,
                    causation_id=persisted.id,
                    trace_id=order.id.hex,
                    payload=OrderStatusAppliedPayload(
                        broker_order_id=persisted.broker_order_id,
                        raw_status=persisted.raw_status,
                        status=order.status,
                        terminal_release_pending=False,
                    ),
                ),
                next_attempt_at=persisted.observed_at,
            )
        self._session.add(
            OpsIncident(
                severity="BLOCKING",
                status="OPEN",
                reason_code="UNSEQUENCED_BROKER_STATUS_ORDERING_AMBIGUITY",
                scope_type="ORDER",
                scope_key=str(order.id),
                created_at=persisted.observed_at,
            )
        )
        await self._session.flush()
        return BrokerStatusApplication(applied=True, terminal_release_pending=False)


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
        status=OrderStatus(order.status),
        aggregate_version=order.aggregate_version,
        broker_client_order_id=order.broker_client_order_id,
        created_at=order.created_at,
    )


def _to_domain_status_event(
    persisted: PersistedBrokerOrderStatusEvent, *, order_id: UUID
) -> BrokerOrderStatusEvent:
    return BrokerOrderStatusEvent(
        broker_id=persisted.broker_id,
        account_id=persisted.account_id,
        source_partition=persisted.source_partition,
        dedupe_key=persisted.dedupe_key,
        raw_status=persisted.raw_status,
        occurred_at=persisted.occurred_at,
        source_sequence=persisted.source_sequence,
        broker_order_id=persisted.broker_order_id,
        order_id=order_id,
        broker_client_order_id=persisted.broker_client_order_id,
        requested_quantity=persisted.requested_quantity,
        cumulative_filled_quantity=persisted.cumulative_filled_quantity,
        observed_at=persisted.observed_at,
        payload_hash=persisted.canonical_payload_hash,
    )
