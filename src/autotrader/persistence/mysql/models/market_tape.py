"""The venue's own trade tape, kept so a restart does not lose the window.

`BinanceUsdmMarketData` deduplicates aggregate trades against a store and
resumes from a checkpoint, and the store was a Protocol nobody implemented.
Without it the loop cannot read a trade window at all: the order-flow
observations, the thirty-second ATR and the extreme-delta threshold are all
taken over the tape.

Two tables rather than one. The trades are the evidence; the checkpoint is the
one aggregate-trade id the next fetch resumes from, and deriving it from the
stored rows would give the largest id we happened to keep rather than the last
one we actually saw. Those differ the moment a window is pruned.

This is market data and not account data. `binance_usdm_trade_fact` next door
holds what this account executed, which is a different question with different
retention and a different authority.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary
from autotrader.shared.ids import new_uuid7


class MarketTradePrintRow(CoreBase):
    __tablename__ = "market_binance_usdm_trade"
    __table_args__ = (
        # The provider's id is what deduplication is done on, and the venue
        # reissues nothing.
        UniqueConstraint(
            "symbol", "provider_trade_id", name="uq_market_binance_usdm_trade_id"
        ),
        CheckConstraint(
            "CHAR_LENGTH(symbol) > 0 AND symbol = TRIM(symbol) "
            "AND CHAR_LENGTH(provider_trade_id) > 0 "
            "AND provider_trade_id = TRIM(provider_trade_id)",
            name="ck_market_binance_usdm_trade_text",
        ),
        # A print with no price or no size is not evidence of anything, and
        # the order-flow rules would divide by it.
        CheckConstraint(
            "price > 0 AND quantity > 0",
            name="ck_market_binance_usdm_trade_amounts",
        ),
        # Every read is a time window over one symbol.
        Index(
            "ix_market_binance_usdm_trade_window",
            "symbol",
            "occurred_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    symbol: Mapped[str] = mapped_column(
        String(32, collation="ascii_bin"), nullable=False
    )
    provider_trade_id: Mapped[str] = mapped_column(
        String(64, collation="ascii_bin"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    # Which side crossed. Nullable because the venue can omit it, and the
    # order flow counts an unknown aggressor rather than guessing one.
    buyer_maker: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)


class MarketTapeCheckpointRow(CoreBase):
    __tablename__ = "market_binance_usdm_checkpoint"
    __table_args__ = (
        UniqueConstraint("symbol", name="uq_market_binance_usdm_checkpoint_symbol"),
        CheckConstraint(
            "last_aggregate_trade_id >= 0",
            name="ck_market_binance_usdm_checkpoint_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    symbol: Mapped[str] = mapped_column(
        String(32, collation="ascii_bin"), nullable=False
    )
    last_aggregate_trade_id: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    last_trade_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


__all__ = ("MarketTapeCheckpointRow", "MarketTradePrintRow")
