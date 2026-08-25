from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid7

from hypothesis import given
from hypothesis import strategies as st

from autotrader.risk.reservations import RiskReservation


@given(st.decimals(min_value=0, max_value=1000, places=2))
def test_reservation_accounting_is_preserved_for_any_consumption(
    initial: Decimal,
) -> None:
    reservation = RiskReservation.create(
        id=uuid7(),
        risk_decision_id=uuid7(),
        order_intent_id=uuid7(),
        account_id=uuid7(),
        initial_risk_amount=initial,
        expires_at=datetime(2026, 8, 9, tzinfo=UTC),
    ).consume(initial + Decimal("1"))

    assert reservation.consumed_risk_amount <= reservation.initial_risk_amount
    assert (
        reservation.consumed_risk_amount
        + reservation.remaining_risk_amount
        + reservation.released_risk_amount
        == reservation.initial_risk_amount
    )
