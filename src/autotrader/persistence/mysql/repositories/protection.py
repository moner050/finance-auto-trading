"""Which positions have a stop standing behind them.

The loop stops opening exposure on this answer and the operator's screen shows
it. Two copies of the rule would eventually disagree, and the screen saying
"protected" while the loop refuses to trade is the worse half of that.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.domain.enums import IntentType
from autotrader.persistence.mysql.dispatch_store import ACCEPTED
from autotrader.persistence.mysql.models.intents import PersistedOrderIntent
from autotrader.persistence.mysql.models.orders import (
    PersistedOrder,
    PersistedOrderCommand,
)
from autotrader.persistence.mysql.models.positions import Position


class ProtectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exposed_instruments(self, *, account_id: UUID) -> set[UUID]:
        """Instruments the account is not flat in."""
        rows = await self._session.scalars(
            select(Position.instrument_id).where(
                Position.account_id == account_id,
                Position.quantity != 0,
            )
        )
        return set(rows.all())

    async def protected_instruments(
        self, *, account_id: UUID, among: set[UUID] | None = None
    ) -> set[UUID]:
        """Instruments with a protective order that reached the broker and has
        not been used up.

        A command that never came back ACCEPTED is not protection: dispatch
        records UNKNOWN for a broker it could not get an answer from, and a
        stop that may or may not be resting protects nothing.
        """
        statement = (
            select(PersistedOrder.instrument_id)
            .join(
                PersistedOrderIntent,
                PersistedOrderIntent.id == PersistedOrder.order_intent_id,
            )
            .join(
                PersistedOrderCommand,
                PersistedOrderCommand.order_id == PersistedOrder.id,
            )
            .where(
                PersistedOrder.account_id == account_id,
                PersistedOrderIntent.intent_type == IntentType.PROTECTIVE.value,
                PersistedOrderCommand.result_state == ACCEPTED,
                PersistedOrder.filled_quantity < PersistedOrder.requested_quantity,
            )
        )
        if among is not None:
            statement = statement.where(PersistedOrder.instrument_id.in_(among))
        return set((await self._session.scalars(statement)).all())

    async def unprotected_instruments(self, *, account_id: UUID) -> tuple[UUID, ...]:
        """Open positions with nothing behind them, in a stable order."""
        exposed = await self.exposed_instruments(account_id=account_id)
        if not exposed:
            return ()
        protected = await self.protected_instruments(
            account_id=account_id, among=exposed
        )
        return tuple(sorted(exposed - protected, key=str))


__all__ = ("ProtectionRepository",)
