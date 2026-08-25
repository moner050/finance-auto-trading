from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from autotrader.shared.decimal import require_decimal


class ReservationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PARTIALLY_CONSUMED = "PARTIALLY_CONSUMED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"


@dataclass(frozen=True, slots=True)
class RiskReservation:
    id: UUID
    risk_decision_id: UUID
    order_intent_id: UUID
    account_id: UUID
    initial_risk_amount: Decimal
    consumed_risk_amount: Decimal
    remaining_risk_amount: Decimal
    released_risk_amount: Decimal
    status: ReservationStatus
    expires_at: datetime
    release_reason: str | None

    def __post_init__(self) -> None:
        amounts = (
            self.initial_risk_amount,
            self.consumed_risk_amount,
            self.remaining_risk_amount,
            self.released_risk_amount,
        )
        if any(require_decimal(amount) < 0 for amount in amounts):
            raise ValueError("reservation amounts must be non-negative")
        if (
            self.consumed_risk_amount
            + self.remaining_risk_amount
            + self.released_risk_amount
            != self.initial_risk_amount
        ):
            raise ValueError("reservation accounting invariant is required")

    @classmethod
    def create(
        cls,
        *,
        id: UUID,
        risk_decision_id: UUID,
        order_intent_id: UUID,
        account_id: UUID,
        initial_risk_amount: Decimal,
        expires_at: datetime,
    ) -> RiskReservation:
        amount = require_decimal(initial_risk_amount)
        if amount < 0:
            raise ValueError("initial risk amount must be non-negative")
        return cls(
            id=id,
            risk_decision_id=risk_decision_id,
            order_intent_id=order_intent_id,
            account_id=account_id,
            initial_risk_amount=amount,
            consumed_risk_amount=Decimal(0),
            remaining_risk_amount=amount,
            released_risk_amount=Decimal(0),
            status=ReservationStatus.ACTIVE,
            expires_at=expires_at,
            release_reason=None,
        )

    def consume(self, risk_amount: Decimal) -> RiskReservation:
        amount = require_decimal(risk_amount)
        if amount < 0:
            raise ValueError("consumed risk amount must be non-negative")
        moved = min(amount, self.remaining_risk_amount)
        remaining = self.remaining_risk_amount - moved
        consumed = self.consumed_risk_amount + moved
        status = (
            ReservationStatus.CONSUMED
            if remaining == 0
            else ReservationStatus.PARTIALLY_CONSUMED
        )
        return replace(
            self,
            consumed_risk_amount=consumed,
            remaining_risk_amount=remaining,
            status=status,
        )

    def release(self, reason: str) -> RiskReservation:
        if not reason.strip():
            raise ValueError("release reason is required")
        return replace(
            self,
            remaining_risk_amount=Decimal(0),
            released_risk_amount=self.released_risk_amount + self.remaining_risk_amount,
            status=ReservationStatus.RELEASED,
            release_reason=reason,
        )
