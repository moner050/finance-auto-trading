from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from autotrader.domain.enums import Side


class PositionLifecycleKind(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    UNAUTHORIZED_OBSERVED = "UNAUTHORIZED_OBSERVED"


@dataclass(frozen=True, slots=True)
class PositionLifecycle:
    position_id: UUID
    lifecycle_ordinal: int
    opening_fill_id: UUID
    opened_at: datetime
    kind: PositionLifecycleKind
    closing_fill_id: UUID | None = None
    closed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PositionLifecycleTransition:
    previous_quantity: Decimal
    next_quantity: Decimal
    opened: PositionLifecycle | None
    closed: PositionLifecycle | None


def apply_lifecycle_transition(
    *,
    position_id: UUID,
    previous_quantity: Decimal,
    side: Side,
    fill_quantity: Decimal,
    fill_id: UUID,
    executed_at: datetime,
    next_ordinal: int,
    active_lifecycle: PositionLifecycle | None = None,
) -> PositionLifecycleTransition:
    signed_fill = fill_quantity if side is Side.BUY else -fill_quantity
    next_quantity = previous_quantity + signed_fill
    opened = None
    closed = None
    if previous_quantity == 0 and next_quantity != 0:
        opened = PositionLifecycle(
            position_id=position_id,
            lifecycle_ordinal=next_ordinal,
            opening_fill_id=fill_id,
            opened_at=executed_at,
            kind=PositionLifecycleKind.AUTHORIZED,
        )
    elif previous_quantity != 0 and (
        next_quantity == 0 or previous_quantity * next_quantity < 0
    ):
        if active_lifecycle is None:
            raise ValueError("an open position requires its active lifecycle")
        closed = PositionLifecycle(
            position_id=position_id,
            lifecycle_ordinal=active_lifecycle.lifecycle_ordinal,
            opening_fill_id=active_lifecycle.opening_fill_id,
            opened_at=active_lifecycle.opened_at,
            kind=active_lifecycle.kind,
            closing_fill_id=fill_id,
            closed_at=executed_at,
        )
        if next_quantity != 0:
            opened = PositionLifecycle(
                position_id=position_id,
                lifecycle_ordinal=next_ordinal,
                opening_fill_id=fill_id,
                opened_at=executed_at,
                kind=PositionLifecycleKind.UNAUTHORIZED_OBSERVED,
            )
    return PositionLifecycleTransition(
        previous_quantity=previous_quantity,
        next_quantity=next_quantity,
        opened=opened,
        closed=closed,
    )
