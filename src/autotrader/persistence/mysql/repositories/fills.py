from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast
from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.contracts.envelope import EventEnvelope
from autotrader.contracts.execution_events import FillAppliedPayload
from autotrader.domain.enums import Side
from autotrader.execution.fills.models import BrokerExecutionEvent, Fill
from autotrader.execution.fills.service import FillApplication
from autotrader.execution.positions.lifecycle import (
    PositionLifecycle,
    PositionLifecycleKind,
    apply_lifecycle_transition,
)
from autotrader.persistence.mysql.models.fills import (
    PersistedFill,
    PersistedFillChargeComponent,
)
from autotrader.persistence.mysql.models.intents import PersistedRiskReservation
from autotrader.persistence.mysql.models.operations import OpsIncident
from autotrader.persistence.mysql.models.orders import (
    PersistedBrokerOrderLink,
    PersistedOrder,
    PersistedOrderEvent,
)
from autotrader.persistence.mysql.models.positions import (
    PersistedPositionLifecycle,
    PersistedPositionLot,
    Position,
)
from autotrader.persistence.mysql.repositories.execution_watermarks import (
    ExecutionWatermarkRepository,
)
from autotrader.persistence.mysql.repositories.operations import (
    lock_global_dispatch_guard,
)
from autotrader.persistence.mysql.repositories.outbox import OutboxRepository
from autotrader.persistence.mysql.repositories.risk import RiskBudgetAnchorRepository
from autotrader.shared.decimal import decimal_to_string
from autotrader.shared.ids import new_uuid7


class FillInsertResult(StrEnum):
    INSERTED = "INSERTED"
    DUPLICATE = "DUPLICATE"
    PAYLOAD_CONFLICT = "PAYLOAD_CONFLICT"


class FillRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def persist_once(
        self, fill: PersistedFill, components: tuple[PersistedFillChargeComponent, ...]
    ) -> tuple[PersistedFill, FillInsertResult]:
        await lock_global_dispatch_guard(self._session)
        result = cast(
            CursorResult[object],
            await self._session.execute(
                insert(PersistedFill)
                .values(
                    **{
                        column.name: getattr(fill, column.name)
                        for column in PersistedFill.__table__.columns
                        if column.name != "id"
                    }
                )
                .prefix_with("IGNORE")
            ),
        )
        existing = await self._session.scalar(
            select(PersistedFill)
            .where(
                PersistedFill.broker_id == fill.broker_id,
                PersistedFill.account_id == fill.account_id,
                PersistedFill.broker_execution_id == fill.broker_execution_id,
            )
            .with_for_update()
        )
        if existing is None:
            raise RuntimeError("inserted fill cannot be read")
        if existing.canonical_payload_hash != fill.canonical_payload_hash:
            self._session.add(
                OpsIncident(
                    severity="BLOCKING",
                    status="OPEN",
                    reason_code="BROKER_EXECUTION_IDENTITY_PAYLOAD_CONFLICT",
                    scope_type="ORDER",
                    scope_key=str(existing.order_id),
                    created_at=fill.observed_at,
                )
            )
            await self._session.flush()
            return existing, FillInsertResult.PAYLOAD_CONFLICT
        if result.rowcount != 1:
            return existing, FillInsertResult.DUPLICATE
        for component in components:
            self._session.add(
                PersistedFillChargeComponent(
                    fill_id=existing.id,
                    component_ordinal=component.component_ordinal,
                    amount=component.amount,
                    currency=component.currency,
                    settlement_asset=component.settlement_asset,
                    charge_kind=component.charge_kind,
                    effect=component.effect,
                    leg_role=component.leg_role,
                    charge_basis=component.charge_basis,
                    basis_quantity=component.basis_quantity,
                    basis_notional=component.basis_notional,
                )
            )
        await self._session.flush()
        return existing, FillInsertResult.INSERTED


class MySqlFillStore:
    """Applies one broker execution in the caller-owned SQLAlchemy transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._fills = FillRepository(session)

    async def apply_event_once(
        self, event: BrokerExecutionEvent
    ) -> FillApplication | None:
        await lock_global_dispatch_guard(self._session)
        anchors = await RiskBudgetAnchorRepository(
            self._session
        ).lock_global_and_account(account_id=event.account_id, currency=event.currency)
        if anchors is None:
            raise ValueError("risk budget anchors are required before a fill applies")
        order = await self._session.scalar(
            select(PersistedOrder)
            .where(PersistedOrder.id == event.order_id)
            .with_for_update()
        )
        if order is None:
            raise LookupError("canonical order not found")
        if (
            order.account_id != event.account_id
            or order.instrument_id != event.instrument_id
            or order.side != event.side.value
        ):
            raise ValueError("broker execution does not match canonical order")
        links = list(
            (
                await self._session.scalars(
                    select(PersistedBrokerOrderLink)
                    .where(PersistedBrokerOrderLink.order_id == order.id)
                    .order_by(PersistedBrokerOrderLink.link_sequence)
                    .with_for_update()
                )
            ).all()
        )
        if not any(link.broker_order_id == event.broker_order_id for link in links):
            raise ValueError("broker execution is not linked to the canonical order")
        persisted, result = await self._fills.persist_once(
            PersistedFill(
                id=event.id,
                order_id=event.order_id,
                account_id=event.account_id,
                instrument_id=event.instrument_id,
                broker_id=event.broker_id,
                broker_execution_id=event.broker_execution_id,
                broker_order_id=event.broker_order_id,
                source_partition=event.source_partition,
                source_sequence=event.source_sequence,
                side=event.side.value,
                quantity=event.quantity,
                price=event.price,
                currency=event.currency,
                executed_at=event.executed_at,
                observed_at=event.observed_at,
                canonical_payload_hash=event.payload_hash,
            ),
            tuple(
                PersistedFillChargeComponent(
                    fill_id=event.id,
                    component_ordinal=item.component_ordinal,
                    amount=item.amount,
                    currency=item.currency,
                    charge_kind=item.charge_kind,
                    effect=item.effect.value,
                    leg_role=item.leg_role.value,
                    charge_basis=item.charge_basis.value,
                    basis_quantity=item.basis_quantity,
                    basis_notional=item.basis_notional,
                )
                for item in event.charges
            ),
        )
        if result is not FillInsertResult.INSERTED:
            return None
        total = order.filled_quantity + event.quantity
        overfill = total > order.requested_quantity
        order.filled_quantity = total
        order.aggregate_version += 1
        order.status = (
            "UNKNOWN"
            if overfill
            else "FILLED"
            if total == order.requested_quantity
            else "PARTIALLY_FILLED"
        )
        if overfill:
            self._session.add(
                OpsIncident(
                    severity="BLOCKING",
                    status="OPEN",
                    reason_code="ORDER_OVERFILL",
                    scope_type="ORDER",
                    scope_key=str(order.id),
                    created_at=event.observed_at,
                )
            )
        if event.source_sequence is not None:
            await ExecutionWatermarkRepository(self._session).advance_stream_sequence(
                broker_id=event.broker_id,
                account_id=event.account_id,
                source_partition=event.source_partition,
                source_sequence=event.source_sequence,
                now=event.observed_at,
                expires_at=event.observed_at.replace(hour=23, minute=59),
                evidence_hash=event.payload_hash,
            )
        position = await self._session.scalar(
            select(Position)
            .where(
                Position.account_id == event.account_id,
                Position.instrument_id == event.instrument_id,
            )
            .with_for_update()
        )
        if position is None:
            position = Position(
                id=new_uuid7(),
                account_id=event.account_id,
                instrument_id=event.instrument_id,
                quantity=Decimal("0"),
                average_cost=Decimal("0"),
                observed_at=event.observed_at,
                blocking_risk=False,
                currency=event.currency,
                settlement_asset=None,
            )
            self._session.add(position)
        elif position.currency is None and position.settlement_asset is None:
            position.currency = event.currency
        previous_quantity = position.quantity
        signed = event.quantity if event.side.value == "BUY" else -event.quantity
        position.quantity += signed
        if event.side.value == "BUY" and previous_quantity >= 0:
            position.average_cost = (
                (position.average_cost * previous_quantity)
                + (event.price * event.quantity)
            ) / position.quantity
        elif position.quantity == 0:
            position.average_cost = Decimal("0")
        elif previous_quantity == 0 or previous_quantity * position.quantity < 0:
            position.average_cost = event.price
        position.observed_at = event.observed_at
        if position.quantity < 0:
            position.blocking_risk = True
        if event.side.value == "SELL" and previous_quantity > 0:
            remaining_to_close = min(previous_quantity, event.quantity)
            lots = list(
                (
                    await self._session.scalars(
                        select(PersistedPositionLot)
                        .where(
                            PersistedPositionLot.position_id == position.id,
                            PersistedPositionLot.remaining_quantity > 0,
                        )
                        .order_by(
                            PersistedPositionLot.opened_at, PersistedPositionLot.id
                        )
                        .with_for_update()
                    )
                ).all()
            )
            for lot in lots:
                closed = min(lot.remaining_quantity, remaining_to_close)
                lot.remaining_quantity -= closed
                remaining_to_close -= closed
                if remaining_to_close == 0:
                    break
        await self._record_position_lineage(
            position=position,
            previous_quantity=previous_quantity,
            fill_id=persisted.id,
            side=event.side,
            quantity=event.quantity,
            executed_at=event.executed_at,
        )
        reservation = await self._session.scalar(
            select(PersistedRiskReservation)
            .where(PersistedRiskReservation.risk_decision_id == order.risk_decision_id)
            .with_for_update()
        )
        risk_increase_quantity = (
            event.quantity
            if event.side.value == "BUY" and previous_quantity >= 0
            else max(Decimal("0"), event.quantity + previous_quantity)
            if event.side.value == "BUY"
            else Decimal("0")
        )
        if reservation is not None:
            attributable_risk = event.price * risk_increase_quantity
            reclaimed = min(reservation.released_risk_amount, attributable_risk)
            from_remaining = min(
                reservation.remaining_risk_amount, attributable_risk - reclaimed
            )
            consumed = reclaimed + from_remaining
            reservation.consumed_risk_amount += consumed
            reservation.released_risk_amount -= reclaimed
            reservation.remaining_risk_amount -= from_remaining
            reservation.status = (
                "PARTIALLY_CONSUMED"
                if (
                    reservation.remaining_risk_amount
                    or reservation.released_risk_amount
                )
                else "CONSUMED"
            )
            for anchor in anchors:
                anchor.remaining_reservation_amount -= from_remaining
                anchor.position_risk_amount += attributable_risk
                anchor.row_version += 1
            if reclaimed:
                self._session.add(
                    OpsIncident(
                        severity="BLOCKING",
                        status="OPEN",
                        reason_code="LATE_FILL_AFTER_RESERVATION_RELEASE",
                        scope_type="ORDER",
                        scope_key=str(order.id),
                        created_at=event.observed_at,
                    )
                )
            if attributable_risk > consumed:
                self._session.add(
                    OpsIncident(
                        severity="BLOCKING",
                        status="OPEN",
                        reason_code="FILL_RISK_EXCEEDS_RESERVATION",
                        scope_type="ORDER",
                        scope_key=str(order.id),
                        created_at=event.observed_at,
                    )
                )
        order_event = PersistedOrderEvent(
            id=new_uuid7(),
            order_id=order.id,
            aggregate_version=order.aggregate_version,
            status=order.status,
            raw_status="FILL_APPLIED",
            occurred_at=event.executed_at,
        )
        self._session.add(order_event)
        await self._session.flush()
        await OutboxRepository(self._session).enqueue_once(
            EventEnvelope[FillAppliedPayload](
                event_id=order_event.id,
                event_type="execution.fill.applied",
                schema_version=1,
                occurred_at=event.executed_at,
                observed_at=event.observed_at,
                producer="execution-fill-service",
                partition_key=str(order.account_id),
                aggregate_type="Order",
                aggregate_id=order.id,
                aggregate_version=order.aggregate_version,
                correlation_id=order.order_intent_id,
                causation_id=persisted.id,
                trace_id=order.id.hex,
                payload=FillAppliedPayload(
                    broker_execution_id=event.broker_execution_id,
                    quantity=event.quantity,
                    total_filled_quantity=Decimal(decimal_to_string(total.normalize())),
                    overfill=overfill,
                ),
            ),
            next_attempt_at=event.observed_at,
        )
        return FillApplication(
            Fill(
                persisted.id,
                order.id,
                persisted.broker_execution_id,
                persisted.quantity,
                persisted.price,
                event.side,
                persisted.executed_at,
                event.charges,
            ),
            total,
            overfill,
        )

    async def _record_position_lineage(
        self,
        *,
        position: Position,
        previous_quantity: Decimal,
        fill_id: UUID,
        side: Side,
        quantity: Decimal,
        executed_at: datetime,
    ) -> None:
        active = await self._session.scalar(
            select(PersistedPositionLifecycle)
            .where(
                PersistedPositionLifecycle.position_id == position.id,
                PersistedPositionLifecycle.closed_at.is_(None),
            )
            .with_for_update()
        )
        current_ordinal = await self._session.scalar(
            select(
                func.coalesce(func.max(PersistedPositionLifecycle.lifecycle_ordinal), 0)
            )
            .where(PersistedPositionLifecycle.position_id == position.id)
            .with_for_update()
        )
        next_ordinal = int(current_ordinal or 0) + 1
        active_domain = (
            PositionLifecycle(
                position_id=active.position_id,
                lifecycle_ordinal=active.lifecycle_ordinal,
                opening_fill_id=active.opening_fill_id,
                opened_at=active.opened_at,
                kind=PositionLifecycleKind(active.kind),
            )
            if active is not None
            else None
        )
        transition = apply_lifecycle_transition(
            position_id=position.id,
            previous_quantity=previous_quantity,
            side=side,
            fill_quantity=quantity,
            fill_id=fill_id,
            executed_at=executed_at,
            next_ordinal=next_ordinal,
            active_lifecycle=active_domain,
        )
        if active is not None and transition.closed is not None:
            active.closing_fill_id = fill_id
            active.closed_at = executed_at
        if transition.opened is not None:
            self._session.add(
                PersistedPositionLifecycle(
                    id=new_uuid7(),
                    position_id=position.id,
                    lifecycle_ordinal=transition.opened.lifecycle_ordinal,
                    opening_fill_id=fill_id,
                    closing_fill_id=None,
                    opened_at=executed_at,
                    closed_at=None,
                    kind=transition.opened.kind.value,
                )
            )
        lot_quantity = (
            quantity
            if side.value == "BUY" and transition.previous_quantity >= 0
            else max(Decimal("0"), quantity + transition.previous_quantity)
            if side.value == "BUY"
            else Decimal("0")
        )
        if lot_quantity > 0:
            self._session.add(
                PersistedPositionLot(
                    id=new_uuid7(),
                    position_id=position.id,
                    opening_fill_id=fill_id,
                    opened_quantity=lot_quantity,
                    remaining_quantity=lot_quantity,
                    opened_at=executed_at,
                )
            )
