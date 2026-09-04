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

_RUN_FK_TARGET = "binance_usdm_reconciliation_run.id"
_BINDING_FK_TARGET = (
    "exec_provider_account_binding.id",
    "exec_provider_account_binding.account_id",
)


class BinanceUsdmReconciliationRunRow(CoreBase):
    __tablename__ = "binance_usdm_reconciliation_run"
    __table_args__ = (
        UniqueConstraint(
            "binding_id",
            "provider_as_of",
            name="uq_binance_usdm_reconciliation_identity",
        ),
        CheckConstraint(
            "provider_code = 'BINANCE' AND market_code = 'USD-M' "
            "AND symbol = 'BTCUSDT' AND settlement_asset = 'USDT'",
            name="ck_binance_usdm_reconciliation_scope",
        ),
        CheckConstraint(
            "provider_as_of <= started_at AND started_at < completed_at "
            "AND balance_fact_count >= 0 AND position_fact_count >= 0 "
            "AND order_fact_count >= 0 AND algo_order_fact_count >= 0 "
            "AND trade_fact_count >= 0 AND income_fact_count >= 0 "
            "AND configuration_fact_count >= 0 "
            "AND OCTET_LENGTH(fact_digest) = 32",
            name="ck_binance_usdm_reconciliation_values",
        ),
        CheckConstraint(
            "(result = 'COMPLETE' AND balance_fact_count >= 1 "
            "AND position_fact_count >= 1 AND configuration_fact_count = 1 "
            "AND JSON_LENGTH(blockers) = 0) OR "
            "(result = 'PARTIAL' AND JSON_LENGTH(blockers) > 0)",
            name="ck_binance_usdm_reconciliation_result",
        ),
        ForeignKeyConstraint(
            ["binding_id", "account_id"],
            list(_BINDING_FK_TARGET),
            name="fk_binance_usdm_reconciliation_binding",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_binance_usdm_reconciliation_readiness",
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
    market_code: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    settlement_asset: Mapped[str] = mapped_column(
        String(8, collation="ascii_bin"), nullable=False
    )
    provider_as_of: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    result: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    balance_fact_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    position_fact_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    order_fact_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    algo_order_fact_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    trade_fact_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    income_fact_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    configuration_fact_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    fact_digest: Mapped[bytes] = mapped_column(VARBINARY(32), nullable=False)
    blockers: Mapped[list[str]] = mapped_column(JSON(), nullable=False)


class BinanceUsdmBalanceFactRow(CoreBase):
    __tablename__ = "binance_usdm_balance_fact"
    __table_args__ = (
        UniqueConstraint("run_id", "asset", name="uq_binance_usdm_balance_identity"),
        CheckConstraint(
            "asset = 'USDT' AND wallet_balance >= 0 AND available_balance >= 0 "
            "AND maximum_withdraw_amount >= 0 AND updated_at <= captured_at",
            name="ck_binance_usdm_balance_values",
        ),
        ForeignKeyConstraint(
            ["run_id"],
            [_RUN_FK_TARGET],
            name="fk_binance_usdm_balance_fact_run",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    run_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    asset: Mapped[str] = mapped_column(String(8, collation="ascii_bin"), nullable=False)
    wallet_balance: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    available_balance: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    maximum_withdraw_amount: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class BinanceUsdmPositionFactRow(CoreBase):
    __tablename__ = "binance_usdm_position_fact"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "symbol",
            "position_side",
            name="uq_binance_usdm_position_identity",
        ),
        CheckConstraint(
            "symbol = 'BTCUSDT' AND position_side = 'BOTH' "
            "AND margin_asset = 'USDT' AND entry_price >= 0 AND mark_price >= 0 "
            "AND isolated_margin >= 0 AND initial_margin >= 0 "
            "AND maintenance_margin >= 0 AND position_initial_margin >= 0 "
            "AND open_order_initial_margin >= 0 AND updated_at <= captured_at",
            name="ck_binance_usdm_position_values",
        ),
        ForeignKeyConstraint(
            ["run_id"],
            [_RUN_FK_TARGET],
            name="fk_binance_usdm_position_fact_run",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_binance_usdm_position_symbol",
            "symbol",
            "captured_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    run_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    symbol: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    position_side: Mapped[str] = mapped_column(
        String(8, collation="ascii_bin"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    mark_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    isolated_margin: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    notional: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    margin_asset: Mapped[str] = mapped_column(
        String(8, collation="ascii_bin"), nullable=False
    )
    initial_margin: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    maintenance_margin: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    position_initial_margin: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    open_order_initial_margin: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class BinanceUsdmOrderFactRow(CoreBase):
    __tablename__ = "binance_usdm_order_fact"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "provider_order_id", name="uq_binance_usdm_order_identity"
        ),
        UniqueConstraint(
            "run_id",
            "client_order_id",
            name="uq_binance_usdm_order_client_identity",
        ),
        CheckConstraint(
            "symbol = 'BTCUSDT' AND side IN ('BUY', 'SELL') "
            "AND original_quantity > 0 AND executed_quantity >= 0 "
            "AND executed_quantity <= original_quantity",
            name="ck_binance_usdm_order_values",
        ),
        ForeignKeyConstraint(
            ["run_id"],
            [_RUN_FK_TARGET],
            name="fk_binance_usdm_order_fact_run",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_binance_usdm_order_provider_identity",
            "provider_order_id",
            "captured_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    run_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    provider_order_id: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    client_order_id: Mapped[str] = mapped_column(
        String(36, collation="ascii_bin"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32, collation="ascii_bin"), nullable=False
    )
    side: Mapped[str] = mapped_column(String(4, collation="ascii_bin"), nullable=False)
    order_type: Mapped[str] = mapped_column(
        String(32, collation="ascii_bin"), nullable=False
    )
    executed_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    original_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    close_position: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class BinanceUsdmAlgoOrderFactRow(CoreBase):
    __tablename__ = "binance_usdm_algo_order_fact"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "provider_algo_id", name="uq_binance_usdm_algo_identity"
        ),
        UniqueConstraint(
            "run_id",
            "client_algo_id",
            name="uq_binance_usdm_algo_client_identity",
        ),
        CheckConstraint(
            "symbol = 'BTCUSDT' AND side IN ('BUY', 'SELL') "
            "AND quantity >= 0 AND trigger_price > 0",
            name="ck_binance_usdm_algo_values",
        ),
        ForeignKeyConstraint(
            ["run_id"],
            [_RUN_FK_TARGET],
            name="fk_binance_usdm_algo_order_fact_run",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_binance_usdm_algo_provider_identity",
            "provider_algo_id",
            "captured_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    run_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    provider_algo_id: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    client_algo_id: Mapped[str] = mapped_column(
        String(36, collation="ascii_bin"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32, collation="ascii_bin"), nullable=False
    )
    side: Mapped[str] = mapped_column(String(4, collation="ascii_bin"), nullable=False)
    order_type: Mapped[str] = mapped_column(
        String(32, collation="ascii_bin"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    trigger_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    close_position: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class BinanceUsdmTradeFactRow(CoreBase):
    __tablename__ = "binance_usdm_trade_fact"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "provider_trade_id", name="uq_binance_usdm_trade_identity"
        ),
        CheckConstraint(
            "symbol = 'BTCUSDT' AND side IN ('BUY', 'SELL') "
            "AND quantity > 0 AND price > 0 AND commission >= 0 "
            "AND commission_asset = 'USDT' AND occurred_at <= captured_at",
            name="ck_binance_usdm_trade_values",
        ),
        ForeignKeyConstraint(
            ["run_id"],
            [_RUN_FK_TARGET],
            name="fk_binance_usdm_trade_fact_run",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_binance_usdm_trade_provider_identity",
            "provider_trade_id",
            "occurred_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    run_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    provider_trade_id: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    provider_order_id: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    symbol: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    side: Mapped[str] = mapped_column(String(4, collation="ascii_bin"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    commission: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    commission_asset: Mapped[str] = mapped_column(
        String(8, collation="ascii_bin"), nullable=False
    )
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class BinanceUsdmIncomeFactRow(CoreBase):
    __tablename__ = "binance_usdm_income_fact"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "provider_transaction_id",
            name="uq_binance_usdm_income_identity",
        ),
        CheckConstraint(
            "asset = 'USDT' AND CHAR_LENGTH(trade_id) <= 64 "
            "AND (symbol = '' OR symbol = 'BTCUSDT') "
            "AND occurred_at <= captured_at",
            name="ck_binance_usdm_income_values",
        ),
        ForeignKeyConstraint(
            ["run_id"],
            [_RUN_FK_TARGET],
            name="fk_binance_usdm_income_fact_run",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_binance_usdm_income_provider_identity",
            "provider_transaction_id",
            "occurred_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    run_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    provider_transaction_id: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    trade_id: Mapped[str] = mapped_column(
        String(64, collation="ascii_bin"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    income_type: Mapped[str] = mapped_column(
        String(32, collation="ascii_bin"), nullable=False
    )
    income: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    asset: Mapped[str] = mapped_column(String(8, collation="ascii_bin"), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class BinanceUsdmConfigurationFactRow(CoreBase):
    __tablename__ = "binance_usdm_configuration_fact"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_binance_usdm_configuration_identity"),
        CheckConstraint(
            "position_mode IN ('ONE_WAY', 'HEDGE') "
            "AND margin_type IN ('ISOLATED', 'CROSSED') AND leverage > 0 "
            "AND maximum_notional > 0 AND price_tick_size > 0 "
            "AND minimum_quantity > 0 AND quantity_step_size > 0 "
            "AND minimum_notional > 0",
            name="ck_binance_usdm_configuration_values",
        ),
        ForeignKeyConstraint(
            ["run_id"],
            [_RUN_FK_TARGET],
            name="fk_binance_usdm_configuration_fact_run",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    run_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    position_mode: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    margin_type: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    auto_add_margin: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    leverage: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    can_trade: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    multi_assets_margin: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    transfer_out_enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    maximum_notional: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    price_tick_size: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    minimum_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    quantity_step_size: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    minimum_notional: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class BinanceUsdmNormalOrderRow(CoreBase):
    """One durable record per order command sent to the venue.

    The order service writes this before it sends and finishes it after, so a
    process that dies mid-send leaves evidence that a request may be in
    flight. Recovery reads it back by client order id, which is why that
    column is unique rather than merely indexed: two rows claiming the same
    id would make recovery ambiguous exactly when it must not be.

    `request_body` is kept whole. The digest alone would say a request was
    sent without saying what it said, and a recovery that cannot reconstruct
    the request cannot tell the venue's answer apart from a different order.

    Only submissions land here. Every order this strategy sends through the
    normal-order path is a market order, which has no working state to cancel
    or replace; the one thing that is cancelled is the protective stop, and
    that is an algo order with its own record.
    """

    __tablename__ = "binance_usdm_normal_order"
    __table_args__ = (
        UniqueConstraint(
            "client_order_id", name="uq_binance_usdm_normal_order_client_id"
        ),
        CheckConstraint(
            "state IN ('PREPARED', 'NOT_SENT', 'AMBIGUOUS', "
            "'ACKNOWLEDGED', 'REJECTED', 'UNKNOWN')",
            name="ck_binance_usdm_normal_order_state",
        ),
        CheckConstraint(
            "OCTET_LENGTH(request_digest) = 32 "
            "AND OCTET_LENGTH(request_body) > 0 "
            "AND CHAR_LENGTH(TRIM(client_order_id)) > 0 "
            "AND dispatch_count >= 1 "
            "AND prepared_at < not_after",
            name="ck_binance_usdm_normal_order_values",
        ),
        # A result is the venue's answer, so it exists exactly when there is
        # one. Any other pairing is a record that lost track of itself.
        CheckConstraint(
            "(state = 'ACKNOWLEDGED' AND result IS NOT NULL) OR "
            "(state <> 'ACKNOWLEDGED' AND result IS NULL)",
            name="ck_binance_usdm_normal_order_result",
        ),
        ForeignKeyConstraint(
            ["binding_id", "account_id"],
            list(_BINDING_FK_TARGET),
            name="fk_binance_usdm_normal_order_binding",
            ondelete="RESTRICT",
        ),
        # What recovery scans: the commands that may still be in flight.
        Index(
            "ix_binance_usdm_normal_order_unresolved",
            "binding_id",
            "state",
            "not_after",
        ),
    )

    command_id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    binding_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    client_order_id: Mapped[str] = mapped_column(
        String(36, collation="ascii_bin"), nullable=False
    )
    request_body: Mapped[bytes] = mapped_column(VARBINARY(2048), nullable=False)
    request_digest: Mapped[bytes] = mapped_column(VARBINARY(32), nullable=False)
    prepared_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    not_after: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    dispatch_count: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    result: Mapped[dict[str, object] | None] = mapped_column(JSON(), nullable=True)


class BinanceUsdmAlgoOrderRow(CoreBase):
    """The protective stop's durable record, written before it is placed.

    A position without a working stop is the one state this system may never
    sit in silently, so the row exists before the request leaves and carries
    `protection_deadline` - the instant by which a stop must be confirmed or
    the position closed instead.

    `EntryFill` is spread into columns rather than kept as a blob. Every one
    of its fields is something a constraint can hold, and a JSON blob would
    move those checks from the database into whatever happens to read it.
    """

    __tablename__ = "binance_usdm_algo_order"
    __table_args__ = (
        UniqueConstraint("client_algo_id", name="uq_binance_usdm_algo_order_client_id"),
        CheckConstraint(
            "state IN ('PREPARED', 'AMBIGUOUS', 'ACTIVE', "
            "'REJECTED', 'EMERGENCY_CLOSED', 'UNKNOWN')",
            name="ck_binance_usdm_algo_order_state",
        ),
        CheckConstraint(
            "side IN ('BUY', 'SELL') AND symbol = 'BTCUSDT'",
            name="ck_binance_usdm_algo_order_scope",
        ),
        CheckConstraint(
            "OCTET_LENGTH(request_digest) = 32 "
            "AND OCTET_LENGTH(request_body) > 0 "
            "AND CHAR_LENGTH(TRIM(client_algo_id)) > 0 "
            "AND first_fill_quantity > 0 "
            "AND cumulative_quantity_before >= 0 "
            "AND average_fill_price > 0 "
            "AND tick_size > 0 "
            "AND trigger_price > 0",
            name="ck_binance_usdm_algo_order_values",
        ),
        # The deadline is what makes an unprotected position finite, and a
        # preparation that predates its own fill is a clock that moved.
        CheckConstraint(
            "filled_at < protection_deadline AND prepared_at >= filled_at",
            name="ck_binance_usdm_algo_order_window",
        ),
        CheckConstraint(
            "(state IN ('ACTIVE', 'EMERGENCY_CLOSED') AND result IS NOT NULL) OR "
            "(state NOT IN ('ACTIVE', 'EMERGENCY_CLOSED') AND result IS NULL)",
            name="ck_binance_usdm_algo_order_result",
        ),
        ForeignKeyConstraint(
            ["binding_id", "account_id"],
            list(_BINDING_FK_TARGET),
            name="fk_binance_usdm_algo_order_binding",
            ondelete="RESTRICT",
        ),
        # What a recovery pass asks for: protection that is not yet safe, in
        # the order its deadlines fall due.
        Index(
            "ix_binance_usdm_algo_order_unsafe",
            "binding_id",
            "state",
            "protection_deadline",
        ),
    )

    entry_command_id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    client_algo_id: Mapped[str] = mapped_column(
        String(36, collation="ascii_bin"), nullable=False
    )
    binding_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    emergency_close_command_id: Mapped[UUID] = mapped_column(
        UuidBinary(), nullable=False
    )
    side: Mapped[str] = mapped_column(String(4, collation="ascii_bin"), nullable=False)
    symbol: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    first_fill_quantity: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    cumulative_quantity_before: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    average_fill_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    tick_size: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    trigger_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    filled_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    protection_deadline: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    prepared_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    request_body: Mapped[bytes] = mapped_column(VARBINARY(2048), nullable=False)
    request_digest: Mapped[bytes] = mapped_column(VARBINARY(32), nullable=False)
    state: Mapped[str] = mapped_column(
        String(20, collation="ascii_bin"), nullable=False
    )
    result: Mapped[dict[str, object] | None] = mapped_column(JSON(), nullable=True)


__all__ = (
    "BinanceUsdmAlgoOrderFactRow",
    "BinanceUsdmAlgoOrderRow",
    "BinanceUsdmBalanceFactRow",
    "BinanceUsdmConfigurationFactRow",
    "BinanceUsdmIncomeFactRow",
    "BinanceUsdmNormalOrderRow",
    "BinanceUsdmOrderFactRow",
    "BinanceUsdmPositionFactRow",
    "BinanceUsdmReconciliationRunRow",
    "BinanceUsdmTradeFactRow",
)
