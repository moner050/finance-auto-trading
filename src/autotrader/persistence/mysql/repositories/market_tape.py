"""The store `BinanceUsdmMarketData` has always required and never had.

It opens a short session per call because the loop calls it from passes that
own no transaction, and because a market-data read must not sit inside the
transaction that later records a decision - a slow fetch would hold row locks
on the decision tables for no reason.

Deduplication is the venue's trade id, which it does not reissue. `persist`
skips what is already stored rather than failing on it: the same window is
fetched again after any restart, and re-fetching evidence is normal.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.integrations.market_data.binance_usdm import (
    BinanceUsdmMarketCheckpoint,
)
from autotrader.persistence.mysql.models.market_tape import (
    MarketTapeCheckpointRow,
    MarketTradePrintRow,
)
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.order_flow import TradePrint


class MySqlMarketTape:
    """Persisted aggregate trades and the id the next fetch resumes from."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def load_checkpoint(self, symbol: str) -> BinanceUsdmMarketCheckpoint | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(MarketTapeCheckpointRow).where(
                    MarketTapeCheckpointRow.symbol == symbol
                )
            )
            if row is None:
                return None
            return BinanceUsdmMarketCheckpoint(
                symbol=row.symbol,
                last_aggregate_trade_id=int(row.last_aggregate_trade_id),
                last_trade_at=require_utc(row.last_trade_at),
            )

    async def find_trade(
        self, symbol: str, provider_trade_id: str
    ) -> TradePrint | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(MarketTradePrintRow).where(
                    MarketTradePrintRow.symbol == symbol,
                    MarketTradePrintRow.provider_trade_id == provider_trade_id,
                )
            )
            return None if row is None else _print(row)

    async def persist(
        self,
        symbol: str,
        trades: tuple[TradePrint, ...],
        checkpoint: BinanceUsdmMarketCheckpoint,
    ) -> None:
        """The trades and the checkpoint together.

        One transaction: a checkpoint written without its trades would tell
        the next fetch to resume past evidence that was never stored, and
        nothing afterwards could tell that it had.
        """
        async with self._sessions() as session:
            if trades:
                stored = set(
                    (
                        await session.scalars(
                            select(MarketTradePrintRow.provider_trade_id).where(
                                MarketTradePrintRow.symbol == symbol,
                                MarketTradePrintRow.provider_trade_id.in_(
                                    trade.provider_trade_id for trade in trades
                                ),
                            )
                        )
                    ).all()
                )
                session.add_all(
                    MarketTradePrintRow(
                        id=new_uuid7(),
                        symbol=symbol,
                        provider_trade_id=trade.provider_trade_id,
                        occurred_at=require_utc(trade.occurred_at),
                        price=trade.price,
                        quantity=trade.quantity,
                        buyer_maker=trade.buyer_maker,
                    )
                    for trade in trades
                    if trade.provider_trade_id not in stored
                    and trade.price is not None
                    and trade.quantity is not None
                )
            existing = await session.scalar(
                select(MarketTapeCheckpointRow)
                .where(MarketTapeCheckpointRow.symbol == symbol)
                .with_for_update()
            )
            if existing is None:
                session.add(
                    MarketTapeCheckpointRow(
                        id=new_uuid7(),
                        symbol=symbol,
                        last_aggregate_trade_id=checkpoint.last_aggregate_trade_id,
                        last_trade_at=require_utc(checkpoint.last_trade_at),
                    )
                )
            elif checkpoint.last_aggregate_trade_id >= existing.last_aggregate_trade_id:
                existing.last_aggregate_trade_id = checkpoint.last_aggregate_trade_id
                existing.last_trade_at = require_utc(checkpoint.last_trade_at)
            # A lower id is not written. Two instances briefly overlapping
            # would otherwise walk the checkpoint backwards and refetch a
            # window that was already stored.
            await session.commit()

    async def load_trades(
        self, symbol: str, start_at: datetime, end_at: datetime
    ) -> tuple[TradePrint, ...]:
        start = require_utc(start_at)
        end = require_utc(end_at)
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(MarketTradePrintRow)
                    .where(
                        MarketTradePrintRow.symbol == symbol,
                        MarketTradePrintRow.occurred_at >= start,
                        MarketTradePrintRow.occurred_at < end,
                    )
                    .order_by(MarketTradePrintRow.occurred_at)
                )
            ).all()
        return tuple(_print(row) for row in rows)


def _print(row: MarketTradePrintRow) -> TradePrint:
    return TradePrint(
        provider_trade_id=row.provider_trade_id,
        occurred_at=require_utc(row.occurred_at),
        price=row.price,
        quantity=row.quantity,
        buyer_maker=row.buyer_maker,
    )


__all__ = ("MySqlMarketTape",)
