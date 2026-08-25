from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid7

from autotrader.domain.enums import Side
from autotrader.execution.positions.lifecycle import (
    PositionLifecycle,
    PositionLifecycleKind,
    apply_lifecycle_transition,
)


def test_zero_to_nonzero_opens_authorized_lifecycle() -> None:
    transition = apply_lifecycle_transition(
        position_id=uuid7(),
        previous_quantity=Decimal("0"),
        side=Side.BUY,
        fill_quantity=Decimal("2"),
        fill_id=uuid7(),
        executed_at=datetime(2026, 8, 9, tzinfo=UTC),
        next_ordinal=1,
    )

    assert transition.next_quantity == Decimal("2")
    assert transition.opened is not None
    assert transition.opened.kind is PositionLifecycleKind.AUTHORIZED
    assert transition.closed is None


def test_sign_crossing_closes_then_opens_unauthorized_observed_lifecycle() -> None:
    transition = apply_lifecycle_transition(
        position_id=uuid7(),
        previous_quantity=Decimal("2"),
        side=Side.SELL,
        fill_quantity=Decimal("3"),
        fill_id=uuid7(),
        executed_at=datetime(2026, 8, 9, tzinfo=UTC),
        next_ordinal=2,
        active_lifecycle=PositionLifecycle(
            position_id=uuid7(),
            lifecycle_ordinal=1,
            opening_fill_id=uuid7(),
            opened_at=datetime(2026, 8, 8, tzinfo=UTC),
            kind=PositionLifecycleKind.AUTHORIZED,
        ),
    )

    assert transition.next_quantity == Decimal("-1")
    assert transition.closed is not None
    assert transition.opened is not None
    assert transition.opened.kind is PositionLifecycleKind.UNAUTHORIZED_OBSERVED
