from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.core import (
    CoreDataSource,
    CoreExchange,
    CoreInstrument,
    CoreMarket,
)


class CoreReferenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def data_source_by_code(self, code: str) -> CoreDataSource | None:
        return await self._session.scalar(
            select(CoreDataSource).where(CoreDataSource.code == code)
        )

    async def market_by_code(self, code: str) -> CoreMarket | None:
        return await self._session.scalar(
            select(CoreMarket).where(CoreMarket.code == code)
        )

    async def exchange_by_market_code(
        self, market_id: UUID, code: str
    ) -> CoreExchange | None:
        return await self._session.scalar(
            select(CoreExchange).where(
                CoreExchange.market_id == market_id, CoreExchange.code == code
            )
        )

    async def instrument_by_exchange_code(
        self, exchange_id: UUID, code: str
    ) -> CoreInstrument | None:
        return await self._session.scalar(
            select(CoreInstrument).where(
                CoreInstrument.exchange_id == exchange_id, CoreInstrument.code == code
            )
        )
