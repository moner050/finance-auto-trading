from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Numeric,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import VARBINARY
from sqlalchemy.orm import Mapped, mapped_column

from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary
from autotrader.shared.ids import new_uuid7


class DavidV6ManifestRow(CoreBase):
    __tablename__ = "strategy_david_v6_manifest"
    __table_args__ = (
        UniqueConstraint(
            "strategy_version_id",
            "source_sha256",
            "configuration_hash",
            name="uq_strategy_david_v6_manifest_version_hashes",
        ),
        UniqueConstraint(
            "id",
            "strategy_version_id",
            name="uq_strategy_david_v6_manifest_exact_version",
        ),
        CheckConstraint(
            "strategy_code = 'DAVID_TRULLAS_V6' "
            "AND strategy_version = 'v6.0-op-20260824.1'",
            name="ck_strategy_david_v6_manifest_identity",
        ),
        CheckConstraint(
            "OCTET_LENGTH(source_sha256) = 32 "
            "AND OCTET_LENGTH(design_sha256) = 32 "
            "AND OCTET_LENGTH(configuration_hash) = 32",
            name="ck_strategy_david_v6_manifest_hashes",
        ),
        ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_version.id"],
            name="fk_strategy_david_v6_manifest_version",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    strategy_version_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    strategy_code: Mapped[str] = mapped_column(
        String(128, collation="utf8mb4_bin"), nullable=False
    )
    strategy_version: Mapped[str] = mapped_column(
        String(64, collation="utf8mb4_bin"), nullable=False
    )
    source_sha256: Mapped[bytes] = mapped_column(VARBINARY(32), nullable=False)
    design_sha256: Mapped[bytes] = mapped_column(VARBINARY(32), nullable=False)
    configuration_hash: Mapped[bytes] = mapped_column(VARBINARY(32), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class DavidV6DecisionRow(CoreBase):
    __tablename__ = "strategy_david_v6_decision"
    __table_args__ = (
        UniqueConstraint(
            "setup_id",
            "instrument_id",
            "decision_hash",
            name="uq_strategy_david_v6_decision_identity",
        ),
        UniqueConstraint(
            "strategy_signal_id",
            name="uq_strategy_david_v6_decision_signal",
        ),
        CheckConstraint(
            "market IN ('KRX_CASH', 'US_CASH', 'BINANCE_USDM') "
            "AND family IN ('METODO', 'HLIT') "
            "AND grade IN ('REJECT', 'NORMAL', 'A_CANDIDATE', 'A') "
            "AND side IN ('BUY', 'SELL') "
            "AND order_style IN ('MARKET', 'LIMIT') "
            "AND (family = 'HLIT' OR market IN ('KRX_CASH', 'US_CASH'))",
            name="ck_strategy_david_v6_decision_scope",
        ),
        CheckConstraint(
            "completed_evidence_at <= generated_at AND generated_at < valid_until",
            name="ck_strategy_david_v6_decision_times",
        ),
        CheckConstraint(
            "matched_indicator_count >= 0 AND blocker_count >= 0 "
            "AND risk_fraction BETWEEN 0 AND 0.0075 "
            "AND calculated_quantity >= 0",
            name="ck_strategy_david_v6_decision_values",
        ),
        CheckConstraint(
            "(grade = 'REJECT' AND blocker_count > 0 "
            "AND strategy_signal_id IS NULL "
            "AND planned_entry IS NULL AND structural_stop IS NULL "
            "AND target_price IS NULL AND expected_cost IS NULL "
            "AND risk_fraction = 0 AND calculated_quantity = 0) "
            "OR (grade <> 'REJECT' AND blocker_count = 0 "
            "AND strategy_signal_id IS NOT NULL "
            "AND planned_entry > 0 AND structural_stop > 0 AND target_price > 0 "
            "AND expected_cost >= 0 AND risk_fraction > 0 "
            "AND calculated_quantity > 0 "
            "AND ((side = 'BUY' AND structural_stop < planned_entry "
            "AND planned_entry < target_price) "
            "OR (side = 'SELL' AND target_price < planned_entry "
            "AND planned_entry < structural_stop)))",
            name="ck_strategy_david_v6_decision_shape",
        ),
        CheckConstraint(
            "OCTET_LENGTH(source_evidence_manifest_hash) = 32 "
            "AND OCTET_LENGTH(decision_hash) = 32",
            name="ck_strategy_david_v6_decision_hashes",
        ),
        ForeignKeyConstraint(
            ["manifest_id", "strategy_version_id"],
            [
                "strategy_david_v6_manifest.id",
                "strategy_david_v6_manifest.strategy_version_id",
            ],
            name="fk_strategy_david_v6_decision_manifest",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_version.id"],
            name="fk_strategy_david_v6_decision_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["setup_id"],
            ["strategy_setup.id"],
            name="fk_strategy_david_v6_decision_setup",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["feature_snapshot_id"],
            ["strategy_feature_snapshot.id"],
            name="fk_strategy_david_v6_decision_feature_snapshot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["instrument_id"],
            ["core_instrument.id"],
            name="fk_strategy_david_v6_decision_instrument",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["strategy_signal_id"],
            ["strategy_signal.id"],
            name="fk_strategy_david_v6_decision_signal",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    manifest_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    strategy_signal_id: Mapped[UUID | None] = mapped_column(UuidBinary(), nullable=True)
    strategy_version_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    setup_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    feature_snapshot_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    market: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    family: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    grade: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    side: Mapped[str] = mapped_column(String(8, collation="ascii_bin"), nullable=False)
    order_style: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    matched_indicator_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    blocker_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    planned_entry: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    structural_stop: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    risk_fraction: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    calculated_quantity: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    expected_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    source_evidence_hashes: Mapped[list[str]] = mapped_column(JSON(), nullable=False)
    source_evidence_manifest_hash: Mapped[bytes] = mapped_column(
        VARBINARY(32), nullable=False
    )
    completed_evidence_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), nullable=False
    )
    generated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    decision_hash: Mapped[bytes] = mapped_column(VARBINARY(32), nullable=False)


class DavidV6IndicatorRow(CoreBase):
    __tablename__ = "strategy_david_v6_indicator"
    __table_args__ = (
        PrimaryKeyConstraint(
            "decision_id",
            "indicator_key",
            name="pk_strategy_david_v6_indicator",
        ),
        UniqueConstraint(
            "decision_id",
            "ordinal",
            name="uq_strategy_david_v6_indicator_ordinal",
        ),
        CheckConstraint(
            "ordinal >= 0 AND CHAR_LENGTH(indicator_key) BETWEEN 1 AND 128 "
            "AND indicator_key = TRIM(indicator_key) "
            "AND evidence_state = 'AVAILABLE'",
            name="ck_strategy_david_v6_indicator_scope",
        ),
        CheckConstraint(
            "OCTET_LENGTH(evidence_hash) = 32",
            name="ck_strategy_david_v6_indicator_hash",
        ),
        ForeignKeyConstraint(
            ["decision_id"],
            ["strategy_david_v6_decision.id"],
            name="fk_strategy_david_v6_indicator_decision",
            ondelete="RESTRICT",
        ),
    )

    decision_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    ordinal: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    indicator_key: Mapped[str] = mapped_column(
        String(128, collation="utf8mb4_bin"), nullable=False
    )
    mandatory: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    evidence_state: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    evidence_hash: Mapped[bytes] = mapped_column(VARBINARY(32), nullable=False)


class DavidV6BlockerRow(CoreBase):
    __tablename__ = "strategy_david_v6_blocker"
    __table_args__ = (
        PrimaryKeyConstraint(
            "decision_id",
            "blocker_code",
            name="pk_strategy_david_v6_blocker",
        ),
        UniqueConstraint(
            "decision_id",
            "ordinal",
            name="uq_strategy_david_v6_blocker_ordinal",
        ),
        CheckConstraint(
            "ordinal >= 0 AND CHAR_LENGTH(blocker_code) BETWEEN 1 AND 128 "
            "AND blocker_code = TRIM(blocker_code)",
            name="ck_strategy_david_v6_blocker_scope",
        ),
        ForeignKeyConstraint(
            ["decision_id"],
            ["strategy_david_v6_decision.id"],
            name="fk_strategy_david_v6_blocker_decision",
            ondelete="RESTRICT",
        ),
    )

    decision_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    ordinal: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    blocker_code: Mapped[str] = mapped_column(
        String(128, collation="utf8mb4_bin"), nullable=False
    )


class DavidV6PositionMarkRow(CoreBase):
    """One observation already emitted for one position.

    `manage_v6_position` carries four flags - the two fibonacci records and
    the two Shadow partial observations - whose only job is to stop the same
    observation being emitted on every pass. Everything else the manager needs
    is derivable from what the account already stores: the average cost and
    quantity from the position, the lots from the fills, the structural stop
    and the approved risk from the decision that opened it. These four are not
    derivable, because "have we said this already" is not a fact about the
    market.

    Existence is the flag. A unique key rather than a boolean column so a
    second emission collides instead of overwriting, which is the difference
    between a duplicate that is refused and one that is silently absorbed.
    """

    __tablename__ = "strategy_david_v6_position_mark"
    __table_args__ = (
        PrimaryKeyConstraint(
            "position_id",
            "mark",
            name="pk_strategy_david_v6_position_mark",
        ),
        CheckConstraint(
            "CHAR_LENGTH(mark) BETWEEN 1 AND 64 AND mark = TRIM(mark)",
            name="ck_strategy_david_v6_position_mark_scope",
        ),
    )

    position_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    mark: Mapped[str] = mapped_column(String(64, collation="ascii_bin"), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
