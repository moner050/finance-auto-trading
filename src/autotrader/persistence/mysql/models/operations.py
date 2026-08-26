from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary
from autotrader.shared.ids import new_uuid7


class OpsTradingControl(CoreBase):
    __tablename__ = "ops_trading_control"
    __table_args__ = (
        CheckConstraint(
            "scope_type <> '' AND scope_key <> ''", name="ck_ops_trading_control_scope"
        ),
        CheckConstraint(
            "kill_switch_level IN ('NONE', 'BLOCK_NEW_EXPOSURE', 'EMERGENCY')",
            name="ck_ops_trading_control_kill_switch",
        ),
        CheckConstraint(
            "fencing_token >= 0", name="ck_ops_trading_control_fencing_token"
        ),
    )

    scope_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    armed: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    kill_switch_level: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_runtime_instance_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    acquired_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    fencing_token: Mapped[int] = mapped_column(nullable=False, default=0)
    row_version: Mapped[int] = mapped_column(nullable=False, default=1)


class OpsRuntimeInstance(CoreBase):
    __tablename__ = "ops_runtime_instance"
    __table_args__ = (
        CheckConstraint(
            "local_state in ('DISARMED','STANDBY')",
            name="ck_ops_runtime_instance_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    local_state: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


class OpsSchedulerLease(CoreBase):
    __tablename__ = "ops_scheduler_lease"
    __table_args__ = (
        CheckConstraint(
            "fencing_token >= 0", name="ck_ops_scheduler_lease_fencing_token"
        ),
    )

    lease_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_runtime_instance_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    acquired_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    fencing_token: Mapped[int] = mapped_column(nullable=False, default=0)
    row_version: Mapped[int] = mapped_column(nullable=False, default=1)


class OpsIncident(CoreBase):
    __tablename__ = "ops_incident"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('INFO', 'WARNING', 'BLOCKING')",
            name="ck_ops_incident_severity",
        ),
        CheckConstraint(
            "status IN ('OPEN', 'RESOLVED')", name="ck_ops_incident_status"
        ),
        Index("ix_ops_incident_severity_status", "severity", "status"),
        Index("ix_ops_incident_scope", "scope_type", "scope_key"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scope_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=lambda: datetime.now(UTC), nullable=False
    )


class OpsAuditLog(CoreBase):
    __tablename__ = "ops_audit_log"
    __table_args__ = (
        Index(
            "ix_ops_audit_log_scope_occurred_at",
            "scope_type",
            "scope_key",
            "occurred_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_runtime_instance_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    fencing_token: Mapped[int] = mapped_column(nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=lambda: datetime.now(UTC), nullable=False
    )
