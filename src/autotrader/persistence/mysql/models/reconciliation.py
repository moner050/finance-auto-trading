from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BINARY, Boolean, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary
from autotrader.shared.ids import new_uuid7


class PersistedReconciliationRun(CoreBase):
    __tablename__ = "exec_reconciliation_run"
    __table_args__ = (
        UniqueConstraint(
            "broker_id",
            "account_id",
            "snapshot_hash",
            name="uq_exec_reconciliation_run_identity",
        ),
        UniqueConstraint(
            "id", "account_id", name="uq_exec_reconciliation_run_id_account"
        ),
        UniqueConstraint(
            "id",
            "account_id",
            "broker_id",
            name="uq_exec_reconciliation_run_id_account_broker",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    broker_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    snapshot_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean(), nullable=False)


class PersistedReconciliationDiff(CoreBase):
    __tablename__ = "exec_reconciliation_diff"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "diff_key", name="uq_exec_reconciliation_diff_identity"
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    run_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    internal_order_id: Mapped[UUID | None] = mapped_column(UuidBinary(), nullable=True)
    broker_order_id: Mapped[str | None] = mapped_column(
        String(128, collation="ascii_bin"), nullable=True
    )
    broker_execution_id: Mapped[str | None] = mapped_column(
        String(128, collation="ascii_bin"), nullable=True
    )
    diff_key: Mapped[str] = mapped_column(
        String(256, collation="ascii_bin"), nullable=False
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    observed_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
