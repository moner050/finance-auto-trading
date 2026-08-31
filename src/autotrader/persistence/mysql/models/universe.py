"""Universe authorities, staged and activated, with their history kept.

Section 17 says the back office is not an authority source, and section 11.5
refuses a row editor for exactly this table. The unit of change is one whole
snapshot, which is why a member row has no lifecycle of its own: it exists
only as part of the snapshot it was uploaded with, and there is nothing here
to update one with.

State is the timestamps rather than a column beside them. A state column can
disagree with the times it summarises; three timestamps and a marker cannot.
A snapshot with no `activated_at` is staged, one with an `active_marker` is
the authority in force, and one with a `superseded_at` is history that a
point-in-time question can still be answered from.

History is why the previous snapshot is not deleted. "Was this a KOSPI 200
common share on the day that trade was decided" has an answer, and it stops
having one the moment yesterday's list is overwritten.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BINARY,
    BigInteger,
    Boolean,
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


class UniverseSnapshotRow(CoreBase):
    __tablename__ = "universe_snapshot"
    __table_args__ = (
        # One authority per universe per date. A second upload for a date
        # already published is a correction, and it has to replace the row
        # rather than sit beside it looking equally authoritative.
        UniqueConstraint(
            "universe_code", "effective_date", name="uq_universe_snapshot_day"
        ),
        # One in force at a time, by the same marker trick the secret
        # activation table uses: NULL does not collide, so staged and
        # superseded rows are unconstrained.
        UniqueConstraint(
            "universe_code", "active_marker", name="uq_universe_snapshot_active"
        ),
        UniqueConstraint(
            "id", "universe_code", name="uq_universe_snapshot_exact_scope"
        ),
        CheckConstraint(
            "universe_code IN ('KOSPI200', 'SP100', 'BINANCE_USDM')",
            name="ck_universe_snapshot_code",
        ),
        # A universe with no members filters nothing, and a digest of the
        # wrong length is not a digest.
        CheckConstraint(
            "member_count > 0 AND OCTET_LENGTH(content_digest) = 32",
            name="ck_universe_snapshot_content",
        ),
        # The lifecycle, where nothing can write around it. Staged has no
        # activation at all; active has one and no supersession; superseded
        # has both, in that order, and has given up the marker.
        CheckConstraint(
            "(activated_at IS NULL AND activated_by IS NULL "
            "AND superseded_at IS NULL AND active_marker IS NULL) OR "
            "(activated_at >= staged_at AND activated_by IS NOT NULL "
            "AND superseded_at IS NULL AND active_marker = 'ACTIVE') OR "
            "(activated_at >= staged_at AND activated_by IS NOT NULL "
            "AND superseded_at >= activated_at AND active_marker IS NULL)",
            name="ck_universe_snapshot_lifecycle",
        ),
        CheckConstraint(
            "CHAR_LENGTH(source_name) > 0 AND source_name = TRIM(source_name) "
            "AND CHAR_LENGTH(source_reference) > 0 "
            "AND source_reference = TRIM(source_reference) "
            "AND CHAR_LENGTH(staged_by) > 0 AND staged_by = TRIM(staged_by)",
            name="ck_universe_snapshot_provenance",
        ),
        # Point-in-time lookup reads this: the latest activated snapshot whose
        # effective date is on or before the day in question.
        Index(
            "ix_universe_snapshot_point_in_time",
            "universe_code",
            "effective_date",
            "activated_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    universe_code: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    # The day the membership is true as of, not the day it was uploaded.
    effective_date: Mapped[date] = mapped_column(Date(), nullable=False)
    content_digest: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    member_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    source_name: Mapped[str] = mapped_column(String(64), nullable=False)
    source_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    source_published_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    staged_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    staged_by: Mapped[str] = mapped_column(String(255), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    activated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    active_marker: Mapped[str | None] = mapped_column(
        String(6, collation="ascii_bin"), nullable=True
    )


class UniverseSnapshotMemberRow(CoreBase):
    __tablename__ = "universe_snapshot_member"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "symbol", name="uq_universe_snapshot_member_symbol"
        ),
        # A preferred line is a member that is not a common share, and the two
        # blockers say different things. The equity authorities must answer
        # the question; a perpetual has no share class to answer it with.
        CheckConstraint(
            "(universe_code IN ('KOSPI200', 'SP100') AND common_stock IS NOT NULL) "
            "OR (universe_code = 'BINANCE_USDM' AND common_stock IS NULL)",
            name="ck_universe_snapshot_member_share_class",
        ),
        CheckConstraint(
            "CHAR_LENGTH(symbol) > 0 AND symbol = TRIM(symbol) "
            "AND (sector_classification IS NULL "
            "OR (CHAR_LENGTH(sector_classification) > 0 "
            "AND sector_classification = TRIM(sector_classification)))",
            name="ck_universe_snapshot_member_text",
        ),
        # Composite, so a member cannot be attached to a snapshot of another
        # universe than the one its share-class rule was checked against.
        ForeignKeyConstraint(
            ["snapshot_id", "universe_code"],
            ["universe_snapshot.id", "universe_snapshot.universe_code"],
            name="fk_universe_snapshot_member_snapshot",
            ondelete="RESTRICT",
        ),
        Index("ix_universe_snapshot_member_symbol", "symbol"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    snapshot_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    universe_code: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(
        String(32, collation="ascii_bin"), nullable=False
    )
    common_stock: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)
    sector_classification: Mapped[str | None] = mapped_column(String(64), nullable=True)


__all__ = ("UniverseSnapshotMemberRow", "UniverseSnapshotRow")
