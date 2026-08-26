from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BINARY,
    VARBINARY,
    BigInteger,
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


class RiskPolicy(CoreBase):
    __tablename__ = "risk_policy"
    __table_args__ = (UniqueConstraint("code", name="uq_risk_policy_code"),)

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    code: Mapped[str] = mapped_column(
        String(128, collation="utf8mb4_bin"), nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean(), nullable=False)


class RiskPolicyVersion(CoreBase):
    __tablename__ = "risk_policy_version"
    __table_args__ = (
        UniqueConstraint("policy_id", "version", name="uq_risk_policy_version"),
        ForeignKeyConstraint(
            ["policy_id"],
            ["risk_policy.id"],
            name="fk_risk_policy_version_policy",
        ),
        CheckConstraint(
            "(max_total_risk IS NULL AND max_position_value IS NULL "
            "AND max_daily_loss IS NULL AND max_drawdown IS NULL) OR "
            "(max_total_risk >= 0 AND max_position_value >= 0 "
            "AND max_daily_loss >= 0 AND max_drawdown >= 0)",
            name="ck_risk_policy_version_limits_non_negative",
        ),
        CheckConstraint(
            "(max_account_snapshot_age_seconds IS NULL "
            "OR max_account_snapshot_age_seconds >= 0) "
            "AND (max_risk_snapshot_age_seconds IS NULL "
            "OR max_risk_snapshot_age_seconds >= 0) "
            "AND (max_market_data_age_seconds IS NULL "
            "OR max_market_data_age_seconds >= 0) "
            "AND (max_provider_fact_age_seconds IS NULL "
            "OR max_provider_fact_age_seconds >= 0)",
            name="ck_risk_policy_version_age_seconds",
        ),
        CheckConstraint(
            "(normal_risk_fraction IS NULL "
            "AND a_candidate_risk_fraction IS NULL "
            "AND a_risk_fraction IS NULL "
            "AND absolute_trade_risk_fraction IS NULL "
            "AND daily_loss_fraction IS NULL "
            "AND weekly_loss_fraction IS NULL "
            "AND max_consecutive_losses IS NULL "
            "AND max_open_structural_risk_fraction IS NULL "
            "AND account_age_seconds IS NULL AND risk_age_seconds IS NULL "
            "AND quote_age_seconds IS NULL AND provider_age_seconds IS NULL "
            "AND stream_gap_age_seconds IS NULL "
            "AND completed_intraday_bar_arrival_seconds IS NULL "
            "AND daily_requires_authoritative_close IS NULL "
            "AND max_total_risk IS NOT NULL AND max_position_value IS NOT NULL "
            "AND max_daily_loss IS NOT NULL AND max_drawdown IS NOT NULL) OR "
            "(normal_risk_fraction IS NOT NULL "
            "AND a_risk_fraction IS NOT NULL "
            "AND absolute_trade_risk_fraction IS NOT NULL "
            "AND daily_loss_fraction IS NOT NULL "
            "AND weekly_loss_fraction IS NOT NULL "
            "AND max_consecutive_losses IS NOT NULL "
            "AND max_open_structural_risk_fraction IS NOT NULL "
            "AND account_age_seconds IS NOT NULL AND risk_age_seconds IS NOT NULL "
            "AND quote_age_seconds IS NOT NULL AND provider_age_seconds IS NOT NULL "
            "AND completed_intraday_bar_arrival_seconds IS NOT NULL "
            "AND daily_requires_authoritative_close IS NOT NULL "
            "AND max_total_risk IS NULL AND max_position_value IS NULL "
            "AND max_daily_loss IS NULL AND max_drawdown IS NULL "
            "AND max_account_snapshot_age_seconds IS NULL "
            "AND max_risk_snapshot_age_seconds IS NULL "
            "AND max_market_data_age_seconds IS NULL "
            "AND max_provider_fact_age_seconds IS NULL)",
            name="ck_risk_policy_version_v6_shape",
        ),
        CheckConstraint(
            "normal_risk_fraction IS NULL OR "
            "(normal_risk_fraction > 0 AND normal_risk_fraction <= 1 "
            "AND (a_candidate_risk_fraction IS NULL OR "
            "(a_candidate_risk_fraction > 0 AND a_candidate_risk_fraction <= 1)) "
            "AND a_risk_fraction > 0 AND a_risk_fraction <= 1 "
            "AND absolute_trade_risk_fraction > 0 "
            "AND absolute_trade_risk_fraction <= 1 "
            "AND normal_risk_fraction <= absolute_trade_risk_fraction "
            "AND (a_candidate_risk_fraction IS NULL OR "
            "a_candidate_risk_fraction <= absolute_trade_risk_fraction) "
            "AND a_risk_fraction <= absolute_trade_risk_fraction "
            "AND daily_loss_fraction > 0 AND daily_loss_fraction <= 1 "
            "AND weekly_loss_fraction > 0 AND weekly_loss_fraction <= 1 "
            "AND max_consecutive_losses > 0 "
            "AND max_open_structural_risk_fraction > 0 "
            "AND max_open_structural_risk_fraction <= 1 "
            "AND account_age_seconds > 0 AND risk_age_seconds > 0 "
            "AND quote_age_seconds > 0 AND provider_age_seconds > 0 "
            "AND (stream_gap_age_seconds IS NULL OR stream_gap_age_seconds > 0) "
            "AND completed_intraday_bar_arrival_seconds > 0)",
            name="ck_risk_policy_version_v6_bounds",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    policy_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    max_total_risk: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    max_position_value: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    max_daily_loss: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    max_drawdown: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    normal_risk_fraction: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 10), nullable=True
    )
    a_candidate_risk_fraction: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 10), nullable=True
    )
    a_risk_fraction: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 10), nullable=True
    )
    absolute_trade_risk_fraction: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 10), nullable=True
    )
    daily_loss_fraction: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 10), nullable=True
    )
    weekly_loss_fraction: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 10), nullable=True
    )
    max_consecutive_losses: Mapped[int | None] = mapped_column(
        BigInteger(), nullable=True
    )
    max_open_structural_risk_fraction: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 10), nullable=True
    )
    account_age_seconds: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    risk_age_seconds: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    quote_age_seconds: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    provider_age_seconds: Mapped[int | None] = mapped_column(
        BigInteger(), nullable=True
    )
    stream_gap_age_seconds: Mapped[int | None] = mapped_column(
        BigInteger(), nullable=True
    )
    completed_intraday_bar_arrival_seconds: Mapped[int | None] = mapped_column(
        BigInteger(), nullable=True
    )
    daily_requires_authoritative_close: Mapped[bool | None] = mapped_column(
        Boolean(), nullable=True
    )
    max_account_snapshot_age_seconds: Mapped[int | None] = mapped_column(
        BigInteger(), nullable=True
    )
    max_risk_snapshot_age_seconds: Mapped[int | None] = mapped_column(
        BigInteger(), nullable=True
    )
    max_market_data_age_seconds: Mapped[int | None] = mapped_column(
        BigInteger(), nullable=True
    )
    max_provider_fact_age_seconds: Mapped[int | None] = mapped_column(
        BigInteger(), nullable=True
    )


class AccountRiskPolicyBinding(CoreBase):
    __tablename__ = "exec_account_risk_policy_binding"
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id"],
            ["exec_account.id"],
            name="fk_exec_account_policy_binding_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["policy_version_id"],
            ["risk_policy_version.id"],
            name="fk_exec_account_policy_binding_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["previous_binding_id"],
            ["exec_account_risk_policy_binding.id"],
            name="fk_exec_account_policy_binding_previous",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "account_id",
            "policy_version_id",
            name="uq_exec_account_policy_binding_target",
        ),
        UniqueConstraint(
            "id",
            "account_id",
            "policy_version_id",
            name="uq_exec_account_policy_binding_exact_target",
        ),
        UniqueConstraint(
            "account_id",
            "currency",
            "active_marker",
            name="uq_exec_account_policy_binding_active_scope",
        ),
        UniqueConstraint(
            "account_id",
            "settlement_asset",
            "active_marker",
            name="uq_exec_account_policy_binding_active_settlement",
        ),
        CheckConstraint(
            "((currency IN ('KRW', 'USD') AND settlement_asset IS NULL) OR "
            "(currency IS NULL AND settlement_asset = 'USDT')) "
            "AND OCTET_LENGTH(account_scope_hash) = 32",
            name="ck_exec_account_policy_binding_scope",
        ),
        CheckConstraint(
            "(deactivated_at IS NULL AND active_marker = 'ACTIVE') "
            "OR (deactivated_at > activated_at AND active_marker IS NULL)",
            name="ck_exec_account_policy_binding_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    policy_version_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    previous_binding_id: Mapped[UUID | None] = mapped_column(
        UuidBinary(), nullable=True
    )
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    settlement_asset: Mapped[str | None] = mapped_column(
        String(16, collation="ascii_bin"),
        nullable=True,
    )
    account_scope_hash: Mapped[bytes] = mapped_column(VARBINARY(32), nullable=False)
    activated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    active_marker: Mapped[str | None] = mapped_column(
        String(6, collation="utf8mb4_bin"), nullable=True
    )


class RiskSnapshot(CoreBase):
    __tablename__ = "risk_snapshot"
    __table_args__ = (
        CheckConstraint(
            "(currency IS NOT NULL AND settlement_asset IS NULL) OR "
            "(currency IS NULL AND settlement_asset IS NOT NULL)",
            name="ck_risk_snapshot_denomination",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    account_snapshot_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    as_of: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    settlement_asset: Mapped[str | None] = mapped_column(String(16), nullable=True)
    equity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    cash: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    gross_exposure: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    net_exposure: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    open_risk: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    daily_realized_pnl: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    daily_unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    drawdown: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    position_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    open_order_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)


class RiskBudgetAnchor(CoreBase):
    __tablename__ = "risk_budget_anchor"
    __table_args__ = (
        UniqueConstraint(
            "scope_type", "scope_key", "currency", name="uq_risk_budget_anchor_scope"
        ),
        CheckConstraint(
            "scope_type IN ('GLOBAL', 'ACCOUNT')", name="ck_risk_budget_anchor_scope"
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_key: Mapped[str] = mapped_column(
        String(128, collation="utf8mb4_bin"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    position_risk_amount: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    remaining_reservation_amount: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    hard_limit_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    row_version: Mapped[int] = mapped_column(BigInteger(), nullable=False)


class DavidV6SessionRiskAnchorRow(CoreBase):
    __tablename__ = "risk_david_v6_session_anchor"
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id"],
            ["exec_account.id"],
            name="fk_risk_david_v6_anchor_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["policy_version_id"],
            ["risk_policy_version.id"],
            name="fk_risk_david_v6_anchor_policy",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["account_snapshot_id"],
            ["exec_account_snapshot.id"],
            name="fk_risk_david_v6_anchor_snapshot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["risk_snapshot_id"],
            ["risk_snapshot.id"],
            name="fk_risk_david_v6_anchor_risk_snapshot",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "account_id",
            "market",
            "session_key",
            name="uq_risk_david_v6_anchor_session",
        ),
        CheckConstraint(
            "market IN ('KRX_CASH', 'US_CASH', 'BINANCE_USDM') "
            "AND CHAR_LENGTH(session_key) > 0",
            name="ck_risk_david_v6_anchor_scope",
        ),
        CheckConstraint(
            "starting_equity > 0 AND captured_at >= session_started_at "
            "AND OCTET_LENGTH(evidence_hash) = 32",
            name="ck_risk_david_v6_anchor_values",
        ),
        CheckConstraint(
            "(currency IS NOT NULL AND settlement_asset IS NULL) OR "
            "(currency IS NULL AND settlement_asset IS NOT NULL)",
            name="ck_risk_david_v6_anchor_denomination",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    policy_version_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    account_snapshot_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    risk_snapshot_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    market: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    session_key: Mapped[str] = mapped_column(
        String(32, collation="ascii_bin"), nullable=False
    )
    session_started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    starting_equity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    settlement_asset: Mapped[str | None] = mapped_column(String(16), nullable=True)
    evidence_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
