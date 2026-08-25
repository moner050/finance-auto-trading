from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BINARY,
    JSON,
    BigInteger,
    CheckConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary
from autotrader.shared.ids import new_uuid7


class OpsOutboxEvent(CoreBase):
    __tablename__ = "ops_outbox_event"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_ops_outbox_event_id"),
        UniqueConstraint(
            "producer",
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
            name="uq_ops_outbox_aggregate_version",
        ),
        Index(
            "ix_ops_outbox_claimable",
            "published_at",
            "next_attempt_at",
            "claim_expires_at",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    event_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    producer: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    aggregate_version: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False)
    payload_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    claim_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=lambda: datetime.now(UTC), nullable=False
    )


class OpsInboxEvent(CoreBase):
    __tablename__ = "ops_inbox_event"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PROCESSED', 'WAITING_FOR_GAP')", name="ck_ops_inbox_status"
        ),
    )

    consumer_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    producer: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    aggregate_version: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    payload_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(String(1024))
    next_attempt_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    deadline_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=lambda: datetime.now(UTC), nullable=False
    )


class OpsInboxDeadLetter(CoreBase):
    __tablename__ = "ops_inbox_dead_letter"
    __table_args__ = (
        UniqueConstraint(
            "consumer_name",
            "event_id",
            "payload_hash",
            name="uq_ops_inbox_dead_letter_identity",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    consumer_name: Mapped[str] = mapped_column(String(128), nullable=False)
    event_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    payload_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=lambda: datetime.now(UTC), nullable=False
    )
