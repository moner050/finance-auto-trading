from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary
from autotrader.shared.ids import new_uuid7


class Position(CoreBase):
    __tablename__ = "exec_position"
    __table_args__ = (
        CheckConstraint(
            "quantity >= 0 OR blocking_risk", name="ck_exec_position_negative_blocking"
        ),
        CheckConstraint(
            "(quantity = 0 AND currency IS NULL AND settlement_asset IS NULL) OR "
            "(currency IS NOT NULL AND settlement_asset IS NULL) OR "
            "(currency IS NULL AND settlement_asset IS NOT NULL)",
            name="ck_exec_position_denomination",
        ),
    )
    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    average_cost: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    settlement_asset: Mapped[str | None] = mapped_column(String(16), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    blocking_risk: Mapped[bool] = mapped_column(Boolean(), nullable=False)


class PersistedPositionLot(CoreBase):
    __tablename__ = "exec_position_lot"
    __table_args__ = (
        UniqueConstraint("opening_fill_id", name="uq_exec_position_lot_opening_fill"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    position_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    opening_fill_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    opened_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    remaining_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class PersistedPositionLifecycle(CoreBase):
    __tablename__ = "exec_position_lifecycle"
    __table_args__ = (
        UniqueConstraint(
            "position_id",
            "lifecycle_ordinal",
            name="uq_exec_position_lifecycle_ordinal",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    position_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    lifecycle_ordinal: Mapped[int] = mapped_column(nullable=False)
    opening_fill_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    closing_fill_id: Mapped[UUID | None] = mapped_column(UuidBinary(), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
