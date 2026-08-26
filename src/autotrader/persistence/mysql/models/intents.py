from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BINARY,
    JSON,
    CheckConstraint,
    Column,
    Computed,
    ForeignKeyConstraint,
    Numeric,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, synonym

from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary
from autotrader.shared.ids import new_uuid7

Table(
    "exec_order_intent_legacy_strategy_link",
    CoreBase.metadata,
    Column("intent_id", UuidBinary(), primary_key=True),
    ForeignKeyConstraint(
        ["intent_id"],
        ["exec_order_intent.id"],
        name="fk_exec_order_intent_legacy_link_intent",
        ondelete="RESTRICT",
    ),
)


class PersistedOrderIntent(CoreBase):
    __tablename__ = "exec_order_intent"
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id"], ["exec_account.id"], name="fk_exec_order_intent_account"
        ),
        ForeignKeyConstraint(
            ["instrument_id"],
            ["core_instrument.id"],
            name="fk_exec_order_intent_instrument",
        ),
        ForeignKeyConstraint(
            ["operator_audit_id"],
            ["ops_audit_log.id"],
            name="fk_exec_order_intent_operator_audit",
        ),
        ForeignKeyConstraint(
            ["protection_position_id"],
            ["exec_position.id"],
            name="fk_exec_order_intent_protection_position",
        ),
        ForeignKeyConstraint(
            ["reconciliation_diff_id"],
            ["exec_reconciliation_diff.id"],
            name="fk_exec_order_intent_reconciliation_diff",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["strategy_signal_id"],
            ["strategy_signal.id"],
            name="fk_exec_order_intent_strategy_signal",
        ),
        CheckConstraint(
            "origin_type in ('STRATEGY','PROTECTION','OPERATOR','RECONCILIATION')",
            name="ck_exec_order_intent_origin",
        ),
        CheckConstraint(
            "(requested_quantity > 0) and ((order_style <> 'LIMIT') or (limit_price > "
            "0))",
            name="ck_exec_order_intent_terms",
        ),
        UniqueConstraint(
            "idempotency_key", name="uq_exec_order_intent_idempotency_key"
        ),
        ForeignKeyConstraint(
            ["legacy_strategy_link_id"],
            ["exec_order_intent_legacy_strategy_link.intent_id"],
            name="fk_exec_order_intent_legacy_strategy_link",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(origin_type = 'STRATEGY' AND "
            "((strategy_signal_id IS NOT NULL "
            "AND legacy_strategy_link_id IS NULL) OR "
            "(strategy_signal_id IS NULL "
            "AND legacy_strategy_link_id IS NOT NULL "
            "AND legacy_strategy_link_id = id)) "
            "AND protection_position_id IS NULL AND operator_audit_id IS NULL "
            "AND reconciliation_diff_id IS NULL) OR "
            "(origin_type = 'PROTECTION' AND strategy_signal_id IS NULL "
            "AND legacy_strategy_link_id IS NULL "
            "AND protection_position_id IS NOT NULL "
            "AND protection_reason_code IS NOT NULL AND operator_audit_id IS NULL "
            "AND reconciliation_diff_id IS NULL) OR "
            "(origin_type = 'OPERATOR' AND strategy_signal_id IS NULL "
            "AND legacy_strategy_link_id IS NULL "
            "AND protection_position_id IS NULL "
            "AND operator_audit_id IS NOT NULL AND reconciliation_diff_id IS NULL) OR "
            "(origin_type = 'RECONCILIATION' AND strategy_signal_id IS NULL "
            "AND legacy_strategy_link_id IS NULL "
            "AND protection_position_id IS NULL "
            "AND operator_audit_id IS NULL AND reconciliation_diff_id IS NOT NULL)",
            name="ck_exec_order_intent_origin_evidence",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    origin_type: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(256, collation="ascii_bin"), nullable=False
    )
    canonical_payload_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    intent_type: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_style: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    # A protective stop rests until the market reaches this price.
    trigger_price: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    strategy_signal_id: Mapped[UUID | None] = mapped_column(UuidBinary(), nullable=True)
    legacy_strategy_link_id: Mapped[UUID | None] = mapped_column(
        UuidBinary(), nullable=True
    )
    eligibility_id: Mapped[UUID | None] = synonym("legacy_strategy_link_id")
    protection_position_id: Mapped[UUID | None] = mapped_column(
        UuidBinary(), nullable=True
    )
    protection_reason_code: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    operator_audit_id: Mapped[UUID | None] = mapped_column(UuidBinary(), nullable=True)
    reconciliation_diff_id: Mapped[UUID | None] = mapped_column(
        UuidBinary(), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class PersistedRiskDecision(CoreBase):
    __tablename__ = "risk_decision"
    __table_args__ = (
        UniqueConstraint("id", "order_intent_id", name="uq_risk_decision_id_intent"),
        ForeignKeyConstraint(
            ["order_intent_id"],
            ["exec_order_intent.id"],
            name="fk_risk_decision_intent",
        ),
        ForeignKeyConstraint(
            ["policy_version_id"],
            ["risk_policy_version.id"],
            name="fk_risk_decision_policy_version",
        ),
        ForeignKeyConstraint(
            ["risk_snapshot_id"], ["risk_snapshot.id"], name="fk_risk_decision_snapshot"
        ),
        CheckConstraint(
            "(outcome in ('APPROVE','REJECT','REDUCE','OBSERVED_BLOCKING')) and "
            "((outcome <> 'OBSERVED_BLOCKING') or ((approved_quantity = 0) and "
            "(reserved_risk_amount > 0)))",
            name="ck_risk_decision_outcome_observed",
        ),
        UniqueConstraint("order_intent_id", name="uq_risk_decision_order_intent"),
        CheckConstraint(
            "requested_quantity > 0 AND approved_quantity >= 0 "
            "AND approved_quantity <= requested_quantity AND reserved_risk_amount >= 0",
            name="ck_risk_decision_quantities",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    order_intent_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    policy_version_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    risk_snapshot_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    requested_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    approved_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    approved_limit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    reserved_risk_amount: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON(), nullable=False)
    decision_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class PersistedRiskReservation(CoreBase):
    __tablename__ = "risk_budget_reservation"
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id"],
            ["exec_account.id"],
            name="fk_risk_budget_reservation_account",
        ),
        ForeignKeyConstraint(
            ["risk_decision_id"],
            ["risk_decision.id"],
            name="fk_risk_budget_reservation_decision",
        ),
        ForeignKeyConstraint(
            ["order_intent_id"],
            ["exec_order_intent.id"],
            name="fk_risk_budget_reservation_intent",
        ),
        CheckConstraint(
            "(initial_risk_amount >= 0) and (consumed_risk_amount >= 0) and "
            "(remaining_risk_amount >= 0) and (released_risk_amount >= 0)",
            name="ck_risk_budget_reservation_amounts_non_negative",
        ),
        CheckConstraint(
            "status in ('ACTIVE','PARTIALLY_CONSUMED','CONSUMED','RELEASED')",
            name="ck_risk_budget_reservation_status",
        ),
        UniqueConstraint(
            "risk_decision_id", name="uq_risk_budget_reservation_decision"
        ),
        UniqueConstraint(
            "order_intent_id",
            "active_marker",
            name="uq_risk_budget_reservation_active_intent",
        ),
        CheckConstraint(
            "consumed_risk_amount + remaining_risk_amount "
            "+ released_risk_amount = initial_risk_amount",
            name="ck_risk_budget_reservation_accounting",
        ),
        CheckConstraint(
            "(currency IS NOT NULL AND settlement_asset IS NULL) OR "
            "(currency IS NULL AND settlement_asset IS NOT NULL)",
            name="ck_risk_budget_reservation_denomination",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    risk_decision_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    order_intent_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    settlement_asset: Mapped[str | None] = mapped_column(String(16), nullable=True)
    initial_risk_amount: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    consumed_risk_amount: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    remaining_risk_amount: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    released_risk_amount: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    active_marker: Mapped[str | None] = mapped_column(
        String(1),
        Computed(
            "CASE WHEN status IN ('ACTIVE', 'PARTIALLY_CONSUMED') "
            "THEN 'Y' ELSE NULL END"
        ),
    )
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    release_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
