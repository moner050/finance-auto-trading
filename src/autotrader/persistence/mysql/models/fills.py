from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BINARY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary
from autotrader.shared.ids import new_uuid7


class PersistedFill(CoreBase):
    __tablename__ = "exec_fill"
    __table_args__ = (
        UniqueConstraint(
            "broker_id",
            "account_id",
            "broker_execution_id",
            name="uq_exec_fill_broker_execution",
        ),
        CheckConstraint(
            "(currency IS NOT NULL AND settlement_asset IS NULL) OR "
            "(currency IS NULL AND settlement_asset IS NOT NULL)",
            name="ck_exec_fill_denomination",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    order_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    broker_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    broker_execution_id: Mapped[str] = mapped_column(
        String(128, collation="ascii_bin"), nullable=False
    )
    broker_order_id: Mapped[str] = mapped_column(
        String(128, collation="ascii_bin"), nullable=False
    )
    source_partition: Mapped[str] = mapped_column(
        String(128, collation="ascii_bin"), nullable=False
    )
    source_sequence: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    settlement_asset: Mapped[str | None] = mapped_column(String(16), nullable=True)
    executed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    canonical_payload_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)


class PersistedFillChargeComponent(CoreBase):
    __tablename__ = "exec_fill_charge_component"
    __table_args__ = (
        UniqueConstraint(
            "fill_id", "component_ordinal", name="uq_exec_fill_charge_component_ordinal"
        ),
        CheckConstraint(
            "(currency IS NOT NULL AND settlement_asset IS NULL) OR "
            "(currency IS NULL AND settlement_asset IS NOT NULL)",
            name="ck_exec_fill_charge_component_denomination",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    fill_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    component_ordinal: Mapped[int] = mapped_column(nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    settlement_asset: Mapped[str | None] = mapped_column(String(16), nullable=True)
    charge_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    effect: Mapped[str] = mapped_column(String(8), nullable=False)
    leg_role: Mapped[str] = mapped_column(String(16), nullable=False)
    charge_basis: Mapped[str] = mapped_column(String(24), nullable=False)
    basis_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    basis_notional: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )


class PersistedExecutionWatermark(CoreBase):
    __tablename__ = "exec_broker_execution_watermark"
    __table_args__ = (
        UniqueConstraint(
            "broker_id",
            "account_id",
            "source_partition",
            name="uq_exec_execution_watermark_partition",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    broker_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    source_partition: Mapped[str] = mapped_column(
        String(128, collation="ascii_bin"), nullable=False
    )
    contiguous_through_sequence: Mapped[int | None] = mapped_column(
        BigInteger(), nullable=True
    )
    has_gap: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    covered_from_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    covered_through_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    pagination_complete: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    query_fingerprint: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    evidence_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    row_version: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    reconciliation_run_id: Mapped[UUID | None] = mapped_column(
        UuidBinary(), nullable=True
    )


class PersistedExecutionGap(CoreBase):
    __tablename__ = "exec_execution_gap"
    __table_args__ = (
        UniqueConstraint(
            "watermark_id",
            "from_sequence",
            "through_sequence",
            name="uq_exec_execution_gap_range",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    watermark_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    from_sequence: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    through_sequence: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)


class PersistedExecutionCheckpointScope(CoreBase):
    __tablename__ = "exec_execution_checkpoint_scope"
    __table_args__ = (
        UniqueConstraint(
            "watermark_id",
            "scope_kind",
            "scope_value",
            name="uq_exec_checkpoint_scope",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    watermark_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    scope_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_value: Mapped[str] = mapped_column(
        String(128, collation="ascii_bin"), nullable=False
    )
