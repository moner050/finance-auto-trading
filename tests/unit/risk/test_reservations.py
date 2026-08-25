from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid7

import pytest

from autotrader.risk.reservations import ReservationStatus, RiskReservation
from autotrader.risk.service import RiskReservationService


@dataclass
class _Anchor:
    position_risk_amount: Decimal
    remaining_reservation_amount: Decimal
    hard_limit_amount: Decimal
    row_version: int


@dataclass
class _Decision:
    id: object
    outcome: str
    reserved_risk_amount: Decimal
    currency: str


@dataclass
class _ReservationRecord:
    risk_decision_id: object
    account_id: object
    initial_risk_amount: Decimal
    remaining_risk_amount: Decimal
    status: str


class _Uow:
    def __init__(self) -> None:
        self.global_anchor = _Anchor(Decimal("0"), Decimal("0"), Decimal("10"), 1)
        self.account_anchor = _Anchor(Decimal("0"), Decimal("0"), Decimal("10"), 1)
        self.persisted: list[object] = []

    async def lock_global_and_account(
        self, *, account_id: object, currency: str
    ) -> tuple[_Anchor, _Anchor]:
        return self.global_anchor, self.account_anchor

    async def persist_decision(self, decision: _Decision) -> _Decision:
        self.persisted.append(decision)
        return decision

    async def persist_reservation(
        self, reservation: _ReservationRecord
    ) -> tuple[object, bool]:
        self.persisted.append(reservation)
        return reservation, True


class _ExistingReservationUow(_Uow):
    async def persist_reservation(
        self, reservation: _ReservationRecord
    ) -> tuple[object, bool]:
        self.persisted.append(reservation)
        return reservation, False


class _CanonicalDecisionUow(_ExistingReservationUow):
    def __init__(self, canonical_id: object) -> None:
        super().__init__()
        self._canonical_id = canonical_id

    async def persist_decision(self, decision: _Decision) -> _Decision:
        self.persisted.append(decision)
        return _Decision(
            id=self._canonical_id,
            outcome=decision.outcome,
            reserved_risk_amount=decision.reserved_risk_amount,
            currency=decision.currency,
        )


def test_reservation_saturates_consumption_and_preserves_accounting() -> None:
    reservation = RiskReservation.create(
        id=uuid7(),
        risk_decision_id=uuid7(),
        order_intent_id=uuid7(),
        account_id=uuid7(),
        initial_risk_amount=Decimal("10"),
        expires_at=datetime(2026, 8, 9, tzinfo=UTC) + timedelta(minutes=1),
    )

    consumed = reservation.consume(Decimal("15"))

    assert consumed.status is ReservationStatus.CONSUMED
    assert consumed.consumed_risk_amount == Decimal("10")
    assert consumed.remaining_risk_amount == Decimal("0")
    assert (
        consumed.consumed_risk_amount
        + consumed.remaining_risk_amount
        + consumed.released_risk_amount
        == Decimal("10")
    )


def test_reservation_release_moves_only_remaining_amount() -> None:
    reservation = RiskReservation.create(
        id=uuid7(),
        risk_decision_id=uuid7(),
        order_intent_id=uuid7(),
        account_id=uuid7(),
        initial_risk_amount=Decimal("10"),
        expires_at=datetime(2026, 8, 9, tzinfo=UTC),
    ).consume(Decimal("4"))

    released = reservation.release("BROKER_TERMINAL_PROVEN")

    assert released.status is ReservationStatus.RELEASED
    assert released.consumed_risk_amount == Decimal("4")
    assert released.released_risk_amount == Decimal("6")
    assert released.remaining_risk_amount == Decimal("0")


@pytest.mark.asyncio
async def test_approval_updates_both_locked_anchors_in_one_scoped_uow() -> None:
    uow = _Uow()
    decision_id = uuid7()
    account_id = uuid7()
    await RiskReservationService(uow=uow).persist_approval(
        decision=_Decision(
            id=decision_id,
            outcome="APPROVE",
            reserved_risk_amount=Decimal("3"),
            currency="USD",
        ),
        reservation=_ReservationRecord(
            risk_decision_id=decision_id,
            account_id=account_id,
            initial_risk_amount=Decimal("3"),
            remaining_risk_amount=Decimal("3"),
            status="ACTIVE",
        ),
        account_id=account_id,
        currency="USD",
    )

    assert uow.global_anchor.remaining_reservation_amount == Decimal("3")
    assert uow.account_anchor.remaining_reservation_amount == Decimal("3")
    assert uow.global_anchor.row_version == uow.account_anchor.row_version == 2
    assert len(uow.persisted) == 2


@pytest.mark.asyncio
async def test_reduction_requires_zero_risk_decision_and_consumed_reservation() -> None:
    uow = _Uow()
    decision_id = uuid7()
    account_id = uuid7()
    with pytest.raises(ValueError, match="zero-risk"):
        await RiskReservationService(uow=uow).persist_approval(
            decision=_Decision(
                id=decision_id,
                outcome="REDUCE",
                reserved_risk_amount=Decimal("1"),
                currency="USD",
            ),
            reservation=_ReservationRecord(
                risk_decision_id=decision_id,
                account_id=account_id,
                initial_risk_amount=Decimal("0"),
                remaining_risk_amount=Decimal("0"),
                status="CONSUMED",
            ),
            account_id=account_id,
            currency="USD",
        )


@pytest.mark.asyncio
async def test_duplicate_reservation_does_not_charge_locked_anchors_twice() -> None:
    uow = _ExistingReservationUow()
    decision_id = uuid7()
    account_id = uuid7()
    await RiskReservationService(uow=uow).persist_approval(
        decision=_Decision(
            id=decision_id,
            outcome="APPROVE",
            reserved_risk_amount=Decimal("3"),
            currency="USD",
        ),
        reservation=_ReservationRecord(
            risk_decision_id=decision_id,
            account_id=account_id,
            initial_risk_amount=Decimal("3"),
            remaining_risk_amount=Decimal("3"),
            status="ACTIVE",
        ),
        account_id=account_id,
        currency="USD",
    )

    assert uow.global_anchor.remaining_reservation_amount == Decimal("0")
    assert uow.account_anchor.remaining_reservation_amount == Decimal("0")


@pytest.mark.asyncio
async def test_duplicate_decision_rebinds_reservation_to_canonical_decision_id() -> (
    None
):
    canonical_id = uuid7()
    uow = _CanonicalDecisionUow(canonical_id)
    account_id = uuid7()
    transient_id = uuid7()
    reservation = _ReservationRecord(
        risk_decision_id=transient_id,
        account_id=account_id,
        initial_risk_amount=Decimal("3"),
        remaining_risk_amount=Decimal("3"),
        status="ACTIVE",
    )
    await RiskReservationService(uow=uow).persist_approval(
        decision=_Decision(
            id=transient_id,
            outcome="APPROVE",
            reserved_risk_amount=Decimal("3"),
            currency="USD",
        ),
        reservation=reservation,
        account_id=account_id,
        currency="USD",
    )

    assert reservation.risk_decision_id == canonical_id
