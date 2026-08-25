from __future__ import annotations

import json
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.contracts.envelope import EventEnvelope
from autotrader.contracts.execution_events import BrokerOrderAdoptedPayload
from autotrader.execution.orders.models import OrderStatus
from autotrader.execution.reconciliation.models import (
    BrokerOpenOrderAdoption,
    BrokerOpenOrderAdoptionResult,
)
from autotrader.persistence.mysql.models.intents import (
    PersistedOrderIntent,
    PersistedRiskDecision,
    PersistedRiskReservation,
)
from autotrader.persistence.mysql.models.operations import OpsAuditLog
from autotrader.persistence.mysql.models.orders import (
    PersistedBrokerOrderLink,
    PersistedOrder,
    PersistedOrderCommandAuthority,
    PersistedOrderEvent,
)
from autotrader.persistence.mysql.models.reconciliation import (
    PersistedReconciliationDiff,
    PersistedReconciliationRun,
)
from autotrader.persistence.mysql.repositories.operations import (
    lock_global_dispatch_guard,
)
from autotrader.persistence.mysql.repositories.outbox import OutboxRepository
from autotrader.persistence.mysql.repositories.risk import RiskBudgetAnchorRepository
from autotrader.shared.ids import new_uuid7


class MySqlBrokerOpenOrderAdoptionStore:
    """Books observed broker exposure in the caller-owned transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def adopt_open_order(
        self, adoption: BrokerOpenOrderAdoption
    ) -> BrokerOpenOrderAdoptionResult:
        canonical_hash = _canonical_adoption_hash(adoption)
        await lock_global_dispatch_guard(self._session)
        diff = await self._session.scalar(
            select(PersistedReconciliationDiff)
            .join(
                PersistedReconciliationRun,
                PersistedReconciliationDiff.run_id == PersistedReconciliationRun.id,
            )
            .where(
                PersistedReconciliationDiff.id == adoption.reconciliation_diff_id,
                PersistedReconciliationRun.account_id == adoption.account_id,
                PersistedReconciliationRun.broker_id == adoption.broker_id,
            )
            .with_for_update()
        )
        if (
            diff is None
            or diff.severity != "BLOCKING"
            or diff.internal_order_id is not None
            or diff.broker_order_id != adoption.broker_order_id
        ):
            raise ValueError("adoption requires a blocking reconciliation diff")
        existing_link = await self._session.scalar(
            select(PersistedBrokerOrderLink)
            .where(
                PersistedBrokerOrderLink.broker_id == adoption.broker_id,
                PersistedBrokerOrderLink.broker_order_id == adoption.broker_order_id,
            )
            .with_for_update()
        )
        if existing_link is not None:
            return await self._existing_result(existing_link, adoption, canonical_hash)
        if diff.status != "OPEN":
            raise ValueError("resolved reconciliation diff has no adopted order")
        anchors = await RiskBudgetAnchorRepository(
            self._session
        ).lock_global_and_account(
            account_id=adoption.account_id, currency=adoption.currency
        )
        if anchors is None:
            raise RuntimeError("adoption requires global and account risk anchors")
        global_anchor, account_anchor = anchors
        intent_id = new_uuid7()
        decision_id = new_uuid7()
        reservation_id = new_uuid7()
        order_id = new_uuid7()
        intent = PersistedOrderIntent(
            id=intent_id,
            origin_type="RECONCILIATION",
            idempotency_key=(
                f"adoption:{adoption.broker_id.hex}:{adoption.broker_order_id}"
            ),
            canonical_payload_hash=canonical_hash,
            account_id=adoption.account_id,
            instrument_id=adoption.instrument_id,
            intent_type="ENTRY",
            side=adoption.side.value,
            order_style=adoption.order_style.value,
            requested_quantity=adoption.requested_quantity,
            limit_price=adoption.limit_price,
            strategy_signal_id=None,
            protection_position_id=None,
            protection_reason_code=None,
            operator_audit_id=None,
            reconciliation_diff_id=diff.id,
            created_at=adoption.observed_at,
        )
        self._session.add(intent)
        await self._session.flush()
        decision = PersistedRiskDecision(
            id=decision_id,
            order_intent_id=intent_id,
            policy_version_id=adoption.policy_version_id,
            risk_snapshot_id=adoption.risk_snapshot_id,
            outcome="OBSERVED_BLOCKING",
            requested_quantity=adoption.requested_quantity,
            approved_quantity=0,
            approved_limit_price=None,
            reserved_risk_amount=adoption.reserved_risk_amount,
            currency=adoption.currency,
            reason_codes=["BROKER_OPEN_ORDER_ADOPTED"],
            decision_hash=sha256(canonical_hash + b":observed").digest(),
            decided_at=adoption.observed_at,
        )
        self._session.add(decision)
        await self._session.flush()
        reservation = PersistedRiskReservation(
            id=reservation_id,
            risk_decision_id=decision_id,
            order_intent_id=intent_id,
            account_id=adoption.account_id,
            currency=adoption.currency,
            settlement_asset=None,
            initial_risk_amount=adoption.reserved_risk_amount,
            consumed_risk_amount=0,
            remaining_risk_amount=adoption.reserved_risk_amount,
            released_risk_amount=0,
            status="ACTIVE",
            expires_at=adoption.reservation_expires_at,
            release_reason=None,
        )
        self._session.add(reservation)
        await self._session.flush()
        order = PersistedOrder(
            id=order_id,
            order_intent_id=intent_id,
            risk_decision_id=decision_id,
            account_id=adoption.account_id,
            instrument_id=adoption.instrument_id,
            broker_client_order_id=adoption.broker_client_order_id,
            side=adoption.side.value,
            order_style=adoption.order_style.value,
            requested_quantity=adoption.requested_quantity,
            filled_quantity=0,
            limit_price=adoption.limit_price,
            status=OrderStatus.UNKNOWN.value,
            aggregate_version=1,
            created_at=adoption.observed_at,
        )
        self._session.add(order)
        await self._session.flush()
        self._session.add_all(
            [
                PersistedBrokerOrderLink(
                    id=new_uuid7(),
                    order_id=order_id,
                    broker_id=adoption.broker_id,
                    broker_order_id=adoption.broker_order_id,
                    link_sequence=1,
                    exposure_bearing=True,
                    status=OrderStatus.UNKNOWN.value,
                ),
                PersistedOrderCommandAuthority(
                    order_id=order_id, authority_class="CANCEL"
                ),
                PersistedOrderEvent(
                    id=new_uuid7(),
                    order_id=order_id,
                    aggregate_version=1,
                    status=OrderStatus.UNKNOWN.value,
                    raw_status="ADOPTED_BROKER_OPEN",
                    occurred_at=adoption.observed_at,
                ),
                OpsAuditLog(
                    action="BROKER_OPEN_ORDER_ADOPTED",
                    scope_type="ORDER",
                    scope_key=str(order_id),
                    actor_runtime_instance_id=None,
                    fencing_token=0,
                    details={
                        "reconciliation_diff_id": str(diff.id),
                        "broker_order_id": adoption.broker_order_id,
                        "raw_payload_hash": adoption.payload_hash.hex(),
                    },
                    occurred_at=adoption.observed_at,
                ),
            ]
        )
        await self._session.flush()
        global_anchor.remaining_reservation_amount += adoption.reserved_risk_amount
        global_anchor.row_version += 1
        account_anchor.remaining_reservation_amount += adoption.reserved_risk_amount
        account_anchor.row_version += 1
        diff.status = "RESOLVED"
        diff.resolved_at = adoption.observed_at
        await OutboxRepository(self._session).enqueue_once(
            EventEnvelope[BrokerOrderAdoptedPayload](
                event_id=new_uuid7(),
                event_type="execution.broker-order.adopted",
                schema_version=1,
                occurred_at=adoption.observed_at,
                observed_at=adoption.observed_at,
                producer="reconciliation-adoption",
                partition_key=str(adoption.account_id),
                aggregate_type="Order",
                aggregate_id=order_id,
                aggregate_version=1,
                correlation_id=diff.id,
                causation_id=diff.id,
                trace_id=order_id.hex,
                payload=BrokerOrderAdoptedPayload(
                    reconciliation_diff_id=str(diff.id),
                    broker_order_id=adoption.broker_order_id,
                    reservation_id=str(reservation_id),
                ),
            ),
            next_attempt_at=adoption.observed_at,
        )
        await self._session.flush()
        return BrokerOpenOrderAdoptionResult(
            order_id, adoption.broker_order_id, reservation_id, True
        )

    async def _existing_result(
        self,
        link: PersistedBrokerOrderLink,
        adoption: BrokerOpenOrderAdoption,
        canonical_hash: bytes,
    ) -> BrokerOpenOrderAdoptionResult:
        order = await self._session.scalar(
            select(PersistedOrder)
            .where(PersistedOrder.id == link.order_id)
            .with_for_update()
        )
        if order is None or (
            order.account_id != adoption.account_id
            or order.instrument_id != adoption.instrument_id
            or order.broker_client_order_id != adoption.broker_client_order_id
            or order.side != adoption.side.value
            or order.order_style != adoption.order_style.value
            or order.requested_quantity != adoption.requested_quantity
            or order.limit_price != adoption.limit_price
        ):
            raise ValueError(
                "broker order adoption payload conflicts with existing evidence"
            )
        intent = await self._session.scalar(
            select(PersistedOrderIntent)
            .where(PersistedOrderIntent.id == order.order_intent_id)
            .with_for_update()
        )
        decision = await self._session.scalar(
            select(PersistedRiskDecision)
            .where(PersistedRiskDecision.id == order.risk_decision_id)
            .with_for_update()
        )
        if (
            intent is None
            or decision is None
            or intent.origin_type != "RECONCILIATION"
            or intent.reconciliation_diff_id != adoption.reconciliation_diff_id
            or intent.canonical_payload_hash != canonical_hash
            or decision.outcome != "OBSERVED_BLOCKING"
            or decision.currency != adoption.currency
            or decision.reserved_risk_amount != adoption.reserved_risk_amount
        ):
            raise ValueError(
                "broker order adoption payload conflicts with existing evidence"
            )
        reservation = await self._session.scalar(
            select(PersistedRiskReservation)
            .where(PersistedRiskReservation.risk_decision_id == order.risk_decision_id)
            .with_for_update()
        )
        if reservation is None:
            raise RuntimeError("adopted order has no reservation")
        return BrokerOpenOrderAdoptionResult(
            order.id, link.broker_order_id, reservation.id, False
        )


def _canonical_adoption_hash(adoption: BrokerOpenOrderAdoption) -> bytes:
    terms = {
        "account_id": str(adoption.account_id),
        "broker_id": str(adoption.broker_id),
        "broker_order_id": adoption.broker_order_id,
        "broker_client_order_id": adoption.broker_client_order_id,
        "currency": adoption.currency,
        "instrument_id": str(adoption.instrument_id),
        "limit_price": str(adoption.limit_price) if adoption.limit_price else None,
        "order_style": adoption.order_style.value,
        "policy_version_id": str(adoption.policy_version_id),
        "reconciliation_diff_id": str(adoption.reconciliation_diff_id),
        "requested_quantity": str(adoption.requested_quantity),
        "reserved_risk_amount": str(adoption.reserved_risk_amount),
        "risk_snapshot_id": str(adoption.risk_snapshot_id),
        "side": adoption.side.value,
    }
    return sha256(
        json.dumps(terms, sort_keys=True, separators=(",", ":")).encode()
    ).digest()
