"""Durable record of internal paper orders.

A paper order is staged when it is sent and resolved when the bar that fills
it closes, which is a later moment. One row carries both so the gap between
them is visible rather than implied, the same shape exec_order_command uses
for its dispatch result.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Numeric,
    String,
)
from sqlalchemy.dialects.mysql import VARBINARY
from sqlalchemy.orm import Mapped, mapped_column

from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary

_STATUSES = "('FILLED', 'PARTIALLY_FILLED', 'NO_FILL', 'UNKNOWN')"
_MARKETS = "('KRX_CASH', 'US_CASH', 'BINANCE_USDM')"


class PaperOrderRow(CoreBase):
    __tablename__ = "exec_paper_order"
    __table_args__ = (
        ForeignKeyConstraint(
            ["order_id"],
            ["exec_order.id"],
            name="fk_exec_paper_order_order",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"market IN {_MARKETS} AND side IN ('BUY', 'SELL') "
            "AND order_style IN ('MARKET', 'LIMIT') "
            "AND quantity > 0 AND timeframe_seconds > 0 "
            "AND fee_per_unit >= 0 AND slippage_per_unit >= 0",
            name="ck_exec_paper_order_terms",
        ),
        CheckConstraint(
            "OCTET_LENGTH(command_digest) = 32",
            name="ck_exec_paper_order_digest",
        ),
        # Either the order is still staged, or it carries a whole receipt.
        CheckConstraint(
            "(status IS NULL AND filled_quantity IS NULL "
            "AND remaining_quantity IS NULL AND fee IS NULL "
            "AND slippage_cost IS NULL) "
            f"OR (status IN {_STATUSES} AND filled_quantity >= 0 "
            "AND remaining_quantity >= 0 AND fee >= 0 AND slippage_cost >= 0 "
            "AND filled_quantity + remaining_quantity = quantity)",
            name="ck_exec_paper_order_receipt",
        ),
        CheckConstraint(
            "(status IS NULL) "
            "OR (status = 'NO_FILL' AND fill_price IS NULL AND filled_at IS NULL "
            "AND reason_code IS NOT NULL AND filled_quantity = 0) "
            "OR (status IN ('FILLED', 'PARTIALLY_FILLED') AND fill_price > 0 "
            "AND filled_at IS NOT NULL AND filled_quantity > 0)",
            name="ck_exec_paper_order_fill",
        ),
    )

    command_id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    order_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    account_alias: Mapped[str] = mapped_column(
        String(64, collation="utf8mb4_bin"), nullable=False
    )
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_style: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    signal_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    timeframe_seconds: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    fee_per_unit: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    slippage_per_unit: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    command_digest: Mapped[bytes] = mapped_column(VARBINARY(32), nullable=False)
    staged_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)

    status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    filled_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    remaining_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    fill_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    fee: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    slippage_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    filled_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_digest: Mapped[bytes | None] = mapped_column(VARBINARY(32), nullable=True)


__all__ = ("PaperOrderRow",)
