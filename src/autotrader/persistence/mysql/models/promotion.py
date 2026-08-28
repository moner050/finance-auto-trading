"""Shadow and Paper sessions, and the evidence each one closed with.

Section 17 says the back office cannot manually edit a promotion state, so the
rule that a session completes only with a verified manifest is written as a
check constraint rather than left to the code that writes the row. A screen, a
CLI and a stray SQL client are all held to it equally, which is the point.

The unique key is (binding, mode, exchange date). Two claims on one date are
that day observed twice, and counting them as two would let a single day
satisfy a requirement that asks for two.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    VARBINARY,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary
from autotrader.shared.ids import new_uuid7

_BINDING_FK_TARGET = (
    "exec_provider_account_binding.id",
    "exec_provider_account_binding.account_id",
)


class PromotionSessionRow(CoreBase):
    __tablename__ = "exec_promotion_session"
    __table_args__ = (
        UniqueConstraint(
            "binding_id",
            "mode",
            "exchange_date",
            name="uq_exec_promotion_session_day",
        ),
        CheckConstraint(
            "mode IN ('SHADOW', 'PAPER') AND status IN ('CLAIMED', 'COMPLETE')",
            name="ck_exec_promotion_session_enums",
        ),
        CheckConstraint(
            "decision_count >= 0 AND order_count >= 0 "
            "AND blocking_incident_count >= 0 "
            "AND blocking_reconciliation_count >= 0 "
            "AND unresolved_unknown_count >= 0",
            name="ck_exec_promotion_session_counts",
        ),
        # The rule, where nothing can write around it. A COMPLETE row must
        # carry evidence that verified: decisions were made, Paper placed
        # orders, and nothing was left unresolved.
        CheckConstraint(
            "(status = 'CLAIMED' AND completed_at IS NULL "
            "AND evidence_digest IS NULL) OR "
            "(status = 'COMPLETE' AND completed_at IS NOT NULL "
            "AND completed_at >= claimed_at "
            "AND OCTET_LENGTH(evidence_digest) = 32 "
            "AND decision_count > 0 "
            "AND (mode = 'SHADOW' OR order_count > 0) "
            "AND blocking_incident_count = 0 "
            "AND blocking_reconciliation_count = 0 "
            "AND unresolved_unknown_count = 0)",
            name="ck_exec_promotion_session_verified",
        ),
        ForeignKeyConstraint(
            ["binding_id", "account_id"],
            list(_BINDING_FK_TARGET),
            name="fk_exec_promotion_session_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["manifest_id"],
            ["strategy_david_v6_manifest.id"],
            name="fk_exec_promotion_session_manifest",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_exec_promotion_session_progress",
            "binding_id",
            "manifest_id",
            "mode",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    binding_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    # The strategy build the day ran under. Sessions of a different build are
    # sessions of a different thing.
    manifest_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    mode: Mapped[str] = mapped_column(String(8, collation="ascii_bin"), nullable=False)
    # A trading day, not an instant.
    exchange_date: Mapped[date] = mapped_column(Date(), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    claimed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    decision_count: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    order_count: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)
    blocking_incident_count: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, default=0
    )
    blocking_reconciliation_count: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, default=0
    )
    unresolved_unknown_count: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, default=0
    )
    # Over the counts the session closed with, so a completed session cannot be
    # quietly re-scored later without the digest disagreeing.
    evidence_digest: Mapped[bytes | None] = mapped_column(VARBINARY(32), nullable=True)


__all__ = ("PromotionSessionRow",)
