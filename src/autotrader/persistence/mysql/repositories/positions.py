from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.positions import Position


class PositionReader:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, position_id: UUID) -> Position | None:
        return await self._session.get(Position, position_id)

    async def ensure_initial_zero(
        self, *, account_id: UUID, instrument_id: UUID, observed_at: datetime
    ) -> Position:
        await self._session.execute(
            insert(Position)
            .values(
                account_id=account_id,
                instrument_id=instrument_id,
                quantity=Decimal("0"),
                average_cost=Decimal("0"),
                observed_at=observed_at,
                blocking_risk=False,
            )
            .prefix_with("IGNORE")
        )
        position = await self._session.scalar(
            select(Position).where(
                Position.account_id == account_id,
                Position.instrument_id == instrument_id,
            )
        )
        if position is None:
            raise RuntimeError("initial position insert did not persist a row")
        return position

    async def lock_for_account_instrument(
        self, *, account_id: UUID, instrument_id: UUID
    ) -> Position | None:
        return await self._session.scalar(
            select(Position)
            .where(
                Position.account_id == account_id,
                Position.instrument_id == instrument_id,
            )
            .with_for_update()
        )
