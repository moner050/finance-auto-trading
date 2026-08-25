from __future__ import annotations

from decimal import Decimal
from typing import Protocol


class RiskDecisionRecord(Protocol):
    id: object
    outcome: str
    reserved_risk_amount: Decimal
    currency: str


class RiskReservationRecord(Protocol):
    risk_decision_id: object
    account_id: object
    initial_risk_amount: Decimal
    remaining_risk_amount: Decimal
    status: str


class MutableBudgetAnchor(Protocol):
    position_risk_amount: Decimal
    remaining_reservation_amount: Decimal
    hard_limit_amount: Decimal
    row_version: int


class RiskReservationUow(Protocol):
    async def lock_global_and_account(
        self, *, account_id: object, currency: str
    ) -> tuple[MutableBudgetAnchor, MutableBudgetAnchor] | None: ...

    async def persist_decision(
        self, decision: RiskDecisionRecord
    ) -> RiskDecisionRecord: ...

    async def persist_reservation(
        self, reservation: RiskReservationRecord
    ) -> tuple[object, bool]: ...


class RiskReservationService:
    """Persists one locked approval with both budget anchors in the caller UoW."""

    def __init__(
        self,
        *,
        uow: RiskReservationUow,
    ) -> None:
        self._uow = uow

    async def persist_approval(
        self,
        *,
        decision: RiskDecisionRecord,
        reservation: RiskReservationRecord,
        account_id: object,
        currency: str,
    ) -> None:
        anchors = await self._uow.lock_global_and_account(
            account_id=account_id, currency=currency
        )
        if anchors is None:
            raise ValueError("locked GLOBAL and ACCOUNT budget anchors are required")
        if reservation.risk_decision_id != decision.id:
            raise ValueError("reservation must belong to the risk decision")
        if reservation.account_id != account_id or decision.currency != currency:
            raise ValueError("reservation account and decision currency must match")
        if decision.outcome == "APPROVE" and (
            reservation.remaining_risk_amount != decision.reserved_risk_amount
        ):
            raise ValueError("reservation amount must equal the approved risk")
        if decision.outcome == "REDUCE" and (
            decision.reserved_risk_amount != Decimal(0)
            or reservation.initial_risk_amount != Decimal(0)
            or reservation.remaining_risk_amount != Decimal(0)
            or reservation.status != "CONSUMED"
        ):
            raise ValueError("reduction requires a zero-risk consumed reservation")
        if decision.outcome not in {"APPROVE", "REDUCE"}:
            raise ValueError("only approved or reduce decisions create a reservation")
        stored_decision = await self._uow.persist_decision(decision)
        reservation.risk_decision_id = stored_decision.id
        _, created = await self._uow.persist_reservation(reservation)
        if not created:
            return
        if decision.outcome == "APPROVE":
            for anchor in anchors:
                if (
                    anchor.position_risk_amount
                    + anchor.remaining_reservation_amount
                    + reservation.remaining_risk_amount
                    > anchor.hard_limit_amount
                ):
                    raise ValueError("risk budget anchor limit exceeded")
                anchor.remaining_reservation_amount += reservation.remaining_risk_amount
                anchor.row_version += 1
