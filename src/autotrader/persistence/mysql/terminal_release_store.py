from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.contracts.envelope import EventEnvelope
from autotrader.contracts.execution_events import ReservationReleasedPayload
from autotrader.execution.fills.terminal_release import decide_terminal_release
from autotrader.execution.orders.models import BrokerOrderLinkState, OrderStatus
from autotrader.persistence.mysql.models.intents import (
    PersistedRiskDecision,
    PersistedRiskReservation,
)
from autotrader.persistence.mysql.models.operations import OpsAuditLog
from autotrader.persistence.mysql.models.orders import (
    PersistedBrokerOrderLink,
    PersistedOrder,
    PersistedOrderEvent,
)
from autotrader.persistence.mysql.repositories.execution_watermarks import (
    ExecutionWatermarkRepository,
)
from autotrader.persistence.mysql.repositories.operations import (
    lock_global_dispatch_guard,
)
from autotrader.persistence.mysql.repositories.outbox import OutboxRepository
from autotrader.persistence.mysql.repositories.risk import RiskBudgetAnchorRepository
from autotrader.shared.ids import new_uuid7


@dataclass(frozen=True, slots=True)
class TerminalReleaseApplication:
    released: bool
    reason: str | None


class MySqlTerminalReleaseStore:
    """Releases terminal residual reservation only on fresh scoped evidence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def release_if_complete(
        self,
        *,
        order_id: UUID,
        broker_id: UUID,
        source_partition: str,
        now: datetime,
    ) -> TerminalReleaseApplication:
        identity = await self._session.scalar(
            select(PersistedOrder).where(PersistedOrder.id == order_id)
        )
        if identity is None:
            raise LookupError("canonical order not found")
        decision = await self._session.scalar(
            select(PersistedRiskDecision).where(
                PersistedRiskDecision.id == identity.risk_decision_id
            )
        )
        if decision is None:
            raise LookupError("risk decision not found")
        await lock_global_dispatch_guard(self._session)
        anchors = await RiskBudgetAnchorRepository(
            self._session
        ).lock_global_and_account(
            account_id=identity.account_id,
            currency=decision.currency,
        )
        if anchors is None:
            raise ValueError("risk budget anchors are required for terminal release")
        order = await self._session.scalar(
            select(PersistedOrder)
            .where(PersistedOrder.id == order_id)
            .with_for_update()
        )
        if order is None:
            raise RuntimeError("locked canonical order cannot be read")
        links = list(
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
        if not links or any(link.broker_id != broker_id for link in links):
            return TerminalReleaseApplication(False, "LINK_SCOPE_UNPROVEN")
        proof = await ExecutionWatermarkRepository(
            self._session
        ).terminal_completeness_proof(
            broker_id=broker_id,
            account_id=order.account_id,
            source_partition=source_partition,
        )
        reservation = await self._session.scalar(
            select(PersistedRiskReservation)
            .where(PersistedRiskReservation.risk_decision_id == order.risk_decision_id)
            .with_for_update()
        )
        if reservation is None:
            raise LookupError("risk reservation not found")
        terminal_at = await self._session.scalar(
            select(func.max(PersistedOrderEvent.occurred_at)).where(
                PersistedOrderEvent.order_id == order.id,
                PersistedOrderEvent.status.in_(
                    [
                        OrderStatus.FILLED.value,
                        OrderStatus.CANCELED.value,
                        OrderStatus.REJECTED.value,
                        OrderStatus.EXPIRED.value,
                    ]
                ),
            )
        )
        if terminal_at is None:
            return TerminalReleaseApplication(False, "LIVE_EXPOSURE_LINK")
        decision_outcome = decide_terminal_release(
            links=tuple(
                BrokerOrderLinkState(
                    id=link.id,
                    broker_order_id=link.broker_order_id,
                    link_sequence=link.link_sequence,
                    exposure_bearing=link.exposure_bearing,
                    status=OrderStatus(link.status),
                )
                for link in links
            ),
            proof=proof,
            broker_client_order_ids=frozenset({order.broker_client_order_id}),
            first_possible_acceptance_at=order.created_at,
            terminal_at=terminal_at,
            now=now,
        )
        if not decision_outcome.release:
            return TerminalReleaseApplication(False, decision_outcome.reason)
        released = reservation.remaining_risk_amount
        if released == 0:
            return TerminalReleaseApplication(False, None)
        for anchor in anchors:
            if anchor.remaining_reservation_amount < released:
                raise ValueError("risk budget anchor cannot release more than reserved")
            anchor.remaining_reservation_amount -= released
            anchor.row_version += 1
        reservation.remaining_risk_amount = Decimal("0")
        reservation.released_risk_amount += released
        reservation.status = "RELEASED"
        release_reason = "BROKER_TERMINAL_EXECUTION_COMPLETE"
        reservation.release_reason = release_reason
        release_event_id = new_uuid7()
        self._session.add(
            OpsAuditLog(
                action="TERMINAL_RESERVATION_RELEASED",
                scope_type="ORDER",
                scope_key=str(order.id),
                actor_runtime_instance_id=None,
                fencing_token=0,
                details={"released_risk_amount": str(released)},
                occurred_at=now,
            )
        )
        await OutboxRepository(self._session).enqueue_once(
            EventEnvelope[ReservationReleasedPayload](
                event_id=release_event_id,
                event_type="execution.reservation.released",
                schema_version=1,
                occurred_at=now,
                observed_at=now,
                producer="execution-terminal-release",
                partition_key=str(order.account_id),
                aggregate_type="Order",
                aggregate_id=order.id,
                aggregate_version=order.aggregate_version,
                correlation_id=order.order_intent_id,
                causation_id=None,
                trace_id=order.id.hex,
                payload=ReservationReleasedPayload(
                    released_risk_amount=released,
                    release_reason=release_reason,
                ),
            ),
            next_attempt_at=now,
        )
        await self._session.flush()
        return TerminalReleaseApplication(True, None)
