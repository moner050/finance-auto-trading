from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import VARBINARY
from sqlalchemy.orm import Mapped, mapped_column

from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary
from autotrader.shared.ids import new_uuid7

_EVIDENCE_STATES = "('AVAILABLE', 'UNAVAILABLE', 'STALE', 'N/A', 'UNKNOWN')"
_ORDER_STATES = (
    "('PENDING', 'PENDING_CANCEL', 'PENDING_REPLACE', 'PARTIAL_FILLED', "
    "'FILLED', 'CANCELED', 'REJECTED', 'CANCEL_REJECTED', "
    "'REPLACE_REJECTED', 'REPLACED')"
)
_BINDING_FK_TARGET = (
    "exec_provider_account_binding.id",
    "exec_provider_account_binding.account_id",
)


class TossUsReconciliationRunRow(CoreBase):
    __tablename__ = "toss_us_reconciliation_run"
    __table_args__ = (
        UniqueConstraint(
            "binding_id",
            "provider_as_of",
            name="uq_toss_us_reconciliation_identity",
        ),
        CheckConstraint(
            "provider_code = 'TOSS' AND market_country = 'US' "
            "AND settlement_asset = 'USD'",
            name="ck_toss_us_reconciliation_scope",
        ),
        CheckConstraint(
            "provider_as_of <= started_at AND started_at <= updated_at "
            "AND (completed_at IS NULL OR updated_at <= completed_at) "
            "AND holdings_page_count >= 0 AND open_order_page_count >= 0 "
            "AND closed_order_page_count >= 0 AND missing_page_count >= 0 "
            "AND cash_fact_count >= 0 AND position_fact_count >= 0 "
            "AND order_fact_count >= 0 "
            "AND (fact_digest IS NULL OR OCTET_LENGTH(fact_digest) = 32)",
            name="ck_toss_us_reconciliation_values",
        ),
        CheckConstraint(
            "(result = 'IN_PROGRESS' AND completed_at IS NULL "
            "AND fact_digest IS NULL AND checkpoint IS NOT NULL "
            "AND JSON_LENGTH(blockers) = 0) OR "
            "(result = 'COMPLETE' AND completed_at > started_at "
            "AND OCTET_LENGTH(fact_digest) = 32 AND checkpoint IS NULL "
            "AND missing_page_count = 0 "
            "AND holdings_page_count >= 1 AND open_order_page_count >= 1 "
            "AND closed_order_page_count >= 1 "
            "AND cash_fact_count = 1 AND JSON_LENGTH(blockers) = 0) OR "
            "(result = 'PARTIAL' AND completed_at > started_at "
            "AND OCTET_LENGTH(fact_digest) = 32 AND checkpoint IS NULL "
            "AND (missing_page_count > 0 "
            "OR holdings_page_count = 0 OR open_order_page_count = 0 "
            "OR closed_order_page_count = 0 OR cash_fact_count = 0 "
            "OR JSON_LENGTH(blockers) > 0))",
            name="ck_toss_us_reconciliation_result",
        ),
        ForeignKeyConstraint(
            ["binding_id", "account_id"],
            list(_BINDING_FK_TARGET),
            name="fk_toss_us_reconciliation_binding",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_toss_us_reconciliation_readiness",
            "binding_id",
            "result",
            "completed_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    binding_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    provider_code: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    market_country: Mapped[str] = mapped_column(
        String(2, collation="ascii_bin"), nullable=False
    )
    settlement_asset: Mapped[str] = mapped_column(
        String(8, collation="ascii_bin"), nullable=False
    )
    provider_as_of: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    result: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    holdings_page_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    open_order_page_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    closed_order_page_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    missing_page_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    cash_fact_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    position_fact_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    order_fact_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    fact_digest: Mapped[bytes | None] = mapped_column(VARBINARY(32), nullable=True)
    blockers: Mapped[list[str]] = mapped_column(JSON(), nullable=False)
    checkpoint: Mapped[dict[str, object] | None] = mapped_column(JSON(), nullable=True)


class TossUsCashFactRow(CoreBase):
    __tablename__ = "toss_us_cash_fact"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "settlement_asset",
            name="uq_toss_us_cash_fact_identity",
        ),
        CheckConstraint(
            f"state IN {_EVIDENCE_STATES} AND settlement_asset = 'USD'",
            name="ck_toss_us_cash_fact_state",
        ),
        CheckConstraint(
            "provider_as_of <= captured_at AND OCTET_LENGTH(source_digest) = 32 "
            "AND ((state = 'AVAILABLE' AND available_cash >= 0 "
            "AND (settled_cash IS NULL OR settled_cash >= 0) "
            "AND CHAR_LENGTH(source_field) BETWEEN 1 AND 128) "
            "OR (state <> 'AVAILABLE' AND available_cash IS NULL "
            "AND settled_cash IS NULL AND source_field IS NULL))",
            name="ck_toss_us_cash_fact_shape",
        ),
        ForeignKeyConstraint(
            ["run_id"],
            ["toss_us_reconciliation_run.id"],
            name="fk_toss_us_cash_fact_run",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    run_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    settlement_asset: Mapped[str] = mapped_column(
        String(8, collation="ascii_bin"), nullable=False
    )
    state: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    available_cash: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    settled_cash: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    source_field: Mapped[str | None] = mapped_column(
        String(128, collation="ascii_bin"), nullable=True
    )
    provider_as_of: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    source_digest: Mapped[bytes] = mapped_column(VARBINARY(32), nullable=False)


class TossUsPositionFactRow(CoreBase):
    __tablename__ = "toss_us_position_fact"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "symbol",
            name="uq_toss_us_position_fact_identity",
        ),
        CheckConstraint(
            "settlement_asset = 'USD' AND CHAR_LENGTH(symbol) BETWEEN 1 AND 32 "
            "AND total_quantity >= 0 AND sellable_quantity >= 0 "
            "AND sellable_quantity <= total_quantity AND average_price >= 0 "
            "AND market_value >= 0",
            name="ck_toss_us_position_fact_values",
        ),
        CheckConstraint(
            "provider_as_of <= captured_at AND OCTET_LENGTH(source_digest) = 32",
            name="ck_toss_us_position_fact_provenance",
        ),
        ForeignKeyConstraint(
            ["run_id"],
            ["toss_us_reconciliation_run.id"],
            name="fk_toss_us_position_fact_run",
            ondelete="RESTRICT",
        ),
        Index("ix_toss_us_position_symbol", "symbol", "provider_as_of"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    run_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    symbol: Mapped[str] = mapped_column(
        String(32, collation="ascii_bin"), nullable=False
    )
    settlement_asset: Mapped[str] = mapped_column(
        String(8, collation="ascii_bin"), nullable=False
    )
    total_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    sellable_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    average_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    market_value: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    provider_as_of: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    source_digest: Mapped[bytes] = mapped_column(VARBINARY(32), nullable=False)


class TossUsOrderFactRow(CoreBase):
    __tablename__ = "toss_us_order_fact"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "provider_order_id",
            name="uq_toss_us_order_fact_identity",
        ),
        CheckConstraint(
            f"side IN ('BUY', 'SELL') AND state IN {_ORDER_STATES} "
            "AND settlement_asset = 'USD' "
            "AND CHAR_LENGTH(provider_order_id) BETWEEN 1 AND 128 "
            "AND CHAR_LENGTH(symbol) BETWEEN 1 AND 32",
            name="ck_toss_us_order_fact_scope",
        ),
        CheckConstraint(
            "quantity > 0 AND cumulative_fill_quantity >= 0 "
            "AND cumulative_fill_quantity <= quantity "
            "AND (limit_price IS NULL OR limit_price > 0) "
            "AND (commission IS NULL OR commission >= 0) "
            "AND (tax IS NULL OR tax >= 0)",
            name="ck_toss_us_order_fact_values",
        ),
        CheckConstraint(
            "ordered_at <= provider_as_of AND provider_as_of <= captured_at "
            "AND (filled_at IS NULL OR filled_at >= ordered_at) "
            "AND (canceled_at IS NULL OR canceled_at >= ordered_at) "
            "AND OCTET_LENGTH(source_digest) = 32",
            name="ck_toss_us_order_fact_provenance",
        ),
        ForeignKeyConstraint(
            ["run_id"],
            ["toss_us_reconciliation_run.id"],
            name="fk_toss_us_order_fact_run",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_toss_us_order_provider_identity",
            "provider_order_id",
            "ordered_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    run_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    provider_order_id: Mapped[str] = mapped_column(
        String(128, collation="ascii_bin"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(
        String(32, collation="ascii_bin"), nullable=False
    )
    side: Mapped[str] = mapped_column(String(4, collation="ascii_bin"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    cumulative_fill_quantity: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    state: Mapped[str] = mapped_column(
        String(32, collation="ascii_bin"), nullable=False
    )
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    commission: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    tax: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    settlement_asset: Mapped[str] = mapped_column(
        String(8, collation="ascii_bin"), nullable=False
    )
    ordered_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    filled_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    provider_as_of: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    source_digest: Mapped[bytes] = mapped_column(VARBINARY(32), nullable=False)


class TossUsRecoveryLeaseRow(CoreBase):
    __tablename__ = "toss_us_recovery_lease"
    __table_args__ = (
        UniqueConstraint(
            "binding_id",
            "client_order_id",
            name="uq_toss_us_recovery_client_order",
        ),
        UniqueConstraint(
            "binding_id",
            "active_marker",
            name="uq_toss_us_recovery_active_binding",
        ),
        CheckConstraint(
            "CHAR_LENGTH(client_order_id) BETWEEN 1 AND 36 "
            "AND OCTET_LENGTH(canonical_request_digest) = 32 "
            "AND replay_count BETWEEN 0 AND 1 "
            "AND terminal_state IN ('OPEN', 'ACKNOWLEDGED', 'REJECTED', "
            "'UNKNOWN', 'EXPIRED') "
            "AND ((terminal_state = 'OPEN' AND active_marker = 'ACTIVE' "
            "AND terminal_at IS NULL AND lease_acquired_at >= first_dispatch_at "
            "AND lease_expires_at > lease_acquired_at "
            "AND lease_expires_at <= DATE_ADD(first_dispatch_at, INTERVAL 600 SECOND)) "
            "OR (terminal_state <> 'OPEN' AND active_marker IS NULL "
            "AND terminal_at >= first_dispatch_at))",
            name="ck_toss_us_recovery_lease_shape",
        ),
        ForeignKeyConstraint(
            ["binding_id", "account_id"],
            list(_BINDING_FK_TARGET),
            name="fk_toss_us_recovery_binding",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_toss_us_recovery_expiry",
            "binding_id",
            "terminal_state",
            "lease_expires_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    binding_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    client_order_id: Mapped[str] = mapped_column(
        String(36, collation="ascii_bin"), nullable=False
    )
    first_dispatch_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    canonical_request_digest: Mapped[bytes] = mapped_column(
        VARBINARY(32), nullable=False
    )
    lease_owner: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    lease_acquired_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    replay_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    terminal_state: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    terminal_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    provider_order_id: Mapped[str | None] = mapped_column(
        String(128, collation="ascii_bin"), nullable=True
    )
    active_marker: Mapped[str | None] = mapped_column(
        String(6, collation="ascii_bin"), nullable=True
    )
