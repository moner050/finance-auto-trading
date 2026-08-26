from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BINARY,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary
from autotrader.shared.ids import new_uuid7


class StrategyDefinition(CoreBase):
    __tablename__ = "strategy_definition"
    __table_args__ = (UniqueConstraint("code", name="uq_strategy_definition_code"),)

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    code: Mapped[str] = mapped_column(
        String(128, collation="utf8mb4_bin"), nullable=False
    )
    research_only: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    configuration_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)


class StrategyVersion(CoreBase):
    __tablename__ = "strategy_version"
    __table_args__ = (
        ForeignKeyConstraint(
            ["definition_id"],
            ["strategy_definition.id"],
            name="fk_strategy_version_definition",
        ),
        UniqueConstraint("definition_id", "version", name="uq_strategy_version_number"),
        CheckConstraint(
            "status IN ('SHADOW', 'LIVE_APPROVED', 'RETIRED')",
            name="ck_strategy_version_status",
        ),
        CheckConstraint(
            "NOT (research_only AND status = 'LIVE_APPROVED')",
            name="ck_strategy_version_research_only",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    definition_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    research_only: Mapped[bool] = mapped_column(Boolean(), nullable=False)


class StrategyRule(CoreBase):
    __tablename__ = "strategy_rule"
    __table_args__ = (
        ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_version.id"],
            name="fk_strategy_rule_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    strategy_version_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    rule_key: Mapped[str] = mapped_column(String(128), nullable=False)
    hard_rule: Mapped[bool] = mapped_column(Boolean(), nullable=False)


class StrategyRuleSource(CoreBase):
    __tablename__ = "strategy_rule_source"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_reference_id"],
            ["strategy_source_reference.id"],
            name="fk_strategy_rule_source_reference",
        ),
        ForeignKeyConstraint(
            ["rule_id"], ["strategy_rule.id"], name="fk_strategy_rule_source_rule"
        ),
    )

    rule_id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    source_reference_id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)


class StrategySourceReference(CoreBase):
    __tablename__ = "strategy_source_reference"
    __table_args__ = (
        UniqueConstraint("source_key", name="uq_strategy_source_reference_key"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    source_key: Mapped[str] = mapped_column(String(128), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean(), nullable=False)


class StrategyFeatureSchema(CoreBase):
    __tablename__ = "strategy_feature_schema"
    __table_args__ = (
        ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_version.id"],
            name="fk_strategy_feature_schema_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    strategy_version_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    schema_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)


class StrategyFeatureSnapshot(CoreBase):
    __tablename__ = "strategy_feature_snapshot"
    __table_args__ = (
        ForeignKeyConstraint(
            ["feature_schema_id"],
            ["strategy_feature_schema.id"],
            name="fk_strategy_feature_snapshot_schema",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    feature_schema_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    payload_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    available_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class StrategySetup(CoreBase):
    __tablename__ = "strategy_setup"
    __table_args__ = (
        ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_version.id"],
            name="fk_strategy_setup_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    strategy_version_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class StrategySignal(CoreBase):
    __tablename__ = "strategy_signal"
    __table_args__ = (
        ForeignKeyConstraint(
            ["feature_snapshot_id"],
            ["strategy_feature_snapshot.id"],
            name="fk_strategy_signal_feature_snapshot",
        ),
        ForeignKeyConstraint(
            ["instrument_id"],
            ["core_instrument.id"],
            name="fk_strategy_signal_instrument",
        ),
        ForeignKeyConstraint(
            ["setup_id"], ["strategy_setup.id"], name="fk_strategy_signal_setup"
        ),
        ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_version.id"],
            name="fk_strategy_signal_version",
        ),
        UniqueConstraint(
            "setup_id", "signal_type", "signal_hash", name="uq_strategy_signal_identity"
        ),
        CheckConstraint(
            "valid_until > generated_at", name="ck_strategy_signal_validity"
        ),
        CheckConstraint(
            "planned_entry_price > 0 AND trigger_price > 0 AND invalidation_price > 0",
            name="ck_strategy_signal_prices_positive",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    strategy_version_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    setup_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    feature_snapshot_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_style: Mapped[str] = mapped_column(String(16), nullable=False)
    planned_entry_price: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    trigger_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    invalidation_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    session_type: Mapped[str] = mapped_column(String(32), nullable=False)
    signal_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
