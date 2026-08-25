from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.intents import (
    PersistedRiskDecision,
    PersistedRiskReservation,
)
from autotrader.persistence.mysql.models.risk import RiskBudgetAnchor


class RiskBudgetAnchorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_global_and_account(
        self, *, account_id: UUID, currency: str
    ) -> tuple[RiskBudgetAnchor, RiskBudgetAnchor] | None:
        global_anchor = await self._session.scalar(
            select(RiskBudgetAnchor)
            .where(
                RiskBudgetAnchor.scope_type == "GLOBAL",
                RiskBudgetAnchor.scope_key == "GLOBAL",
                RiskBudgetAnchor.currency == currency,
            )
            .with_for_update()
        )
        account_anchor = await self._session.scalar(
            select(RiskBudgetAnchor)
            .where(
                RiskBudgetAnchor.scope_type == "ACCOUNT",
                RiskBudgetAnchor.scope_key == str(account_id),
                RiskBudgetAnchor.currency == currency,
            )
            .with_for_update()
        )
        if global_anchor is None or account_anchor is None:
            return None
        return global_anchor, account_anchor


class RiskDecisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def persist(self, decision: PersistedRiskDecision) -> PersistedRiskDecision:
        await self._session.execute(
            insert(PersistedRiskDecision)
            .values(
                id=decision.id,
                order_intent_id=decision.order_intent_id,
                policy_version_id=decision.policy_version_id,
                risk_snapshot_id=decision.risk_snapshot_id,
                outcome=decision.outcome,
                requested_quantity=decision.requested_quantity,
                approved_quantity=decision.approved_quantity,
                approved_limit_price=decision.approved_limit_price,
                reserved_risk_amount=decision.reserved_risk_amount,
                currency=decision.currency,
                reason_codes=decision.reason_codes,
                decision_hash=decision.decision_hash,
                decided_at=decision.decided_at,
            )
            .prefix_with("IGNORE")
        )
        existing = await self._session.scalar(
            select(PersistedRiskDecision)
            .where(PersistedRiskDecision.order_intent_id == decision.order_intent_id)
            .with_for_update()
        )
        if existing is None:
            raise RuntimeError("inserted risk decision cannot be read")
        if existing.decision_hash != decision.decision_hash:
            raise ValueError("risk decision identity payload collision")
        return existing


class RiskReservationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def persist(
        self, reservation: PersistedRiskReservation
    ) -> tuple[PersistedRiskReservation, bool]:
        denomination = await self._session.scalar(
            select(PersistedRiskDecision.currency).where(
                PersistedRiskDecision.id == reservation.risk_decision_id
            )
        )
        if denomination is None:
            raise ValueError("risk reservation requires denomination authority")
        result = await self._session.execute(
            insert(PersistedRiskReservation)
            .values(
                id=reservation.id,
                risk_decision_id=reservation.risk_decision_id,
                order_intent_id=reservation.order_intent_id,
                account_id=reservation.account_id,
                currency=reservation.currency or denomination,
                settlement_asset=reservation.settlement_asset,
                initial_risk_amount=reservation.initial_risk_amount,
                consumed_risk_amount=reservation.consumed_risk_amount,
                remaining_risk_amount=reservation.remaining_risk_amount,
                released_risk_amount=reservation.released_risk_amount,
                status=reservation.status,
                expires_at=reservation.expires_at,
                release_reason=reservation.release_reason,
            )
            .prefix_with("IGNORE")
        )
        existing = await self._session.scalar(
            select(PersistedRiskReservation)
            .where(
                PersistedRiskReservation.risk_decision_id
                == reservation.risk_decision_id
            )
            .with_for_update()
        )
        if existing is None:
            raise RuntimeError("inserted risk reservation cannot be read")
        if (
            existing.order_intent_id != reservation.order_intent_id
            or existing.initial_risk_amount != reservation.initial_risk_amount
            or existing.currency != (reservation.currency or denomination)
            or existing.settlement_asset != reservation.settlement_asset
        ):
            raise ValueError("risk reservation identity payload collision")
        inserted = cast(CursorResult[object], result).rowcount == 1
        return existing, inserted


class MySqlRiskReservationUow:
    """Single-session adapter for the RiskReservationService transaction boundary."""

    def __init__(self, session: AsyncSession) -> None:
        self._anchors = RiskBudgetAnchorRepository(session)
        self._decisions = RiskDecisionRepository(session)
        self._reservations = RiskReservationRepository(session)

    async def lock_global_and_account(
        self, *, account_id: UUID, currency: str
    ) -> tuple[RiskBudgetAnchor, RiskBudgetAnchor] | None:
        return await self._anchors.lock_global_and_account(
            account_id=account_id, currency=currency
        )

    async def persist_decision(
        self, decision: PersistedRiskDecision
    ) -> PersistedRiskDecision:
        return await self._decisions.persist(decision)

    async def persist_reservation(
        self, reservation: PersistedRiskReservation
    ) -> tuple[PersistedRiskReservation, bool]:
        return await self._reservations.persist(reservation)
