"""Carry the venue's own fills into the ledger, then protect what opened.

The paper settlement resolves an order against a bar that has closed. This one
asks Binance what actually filled, which changes three things and leaves the
rest alone.

**The cursor is the ledger.** `exec_fill` already records `source_sequence`
for every trade ingested, so the highest one is exactly where to resume. No
separate cursor to keep in step with the thing it describes.

**The execution watermark is deliberately not used here.** Its model is a
contiguous stream from sequence one, and Binance trade ids are global to the
symbol rather than to this account - starting at one would declare a gap of
billions that does not exist. Deduplication is on the venue's trade id
instead, which is a stronger guarantee than a window that must not overlap.

**A trade with no order of ours is left alone, and counted.** The account can
be traded by hand, and a manual fill has no canonical order to attach to.
Attaching it to something would corrupt the ledger; ignoring it silently would
hide a position this system does not know it has. So it is skipped and
reported, and reconciliation is what says the position drifted.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.trader.composition import (
    LEG_ROLES,
    ExecutionAccount,
    create_protective_order,
    protection_plan,
)
from autotrader.domain.enums import IntentType
from autotrader.execution.dispatch.service import BrokerSubmitter, DispatchService
from autotrader.integrations.brokers.binance_usdm.account import BinanceUsdmTradeFact
from autotrader.integrations.brokers.binance_usdm.fills import (
    SOURCE_PARTITION,
    binance_execution_event,
)
from autotrader.persistence.mysql.dispatch_store import MySqlDispatchStore
from autotrader.persistence.mysql.models.fills import PersistedFill
from autotrader.persistence.mysql.models.intents import PersistedOrderIntent
from autotrader.persistence.mysql.models.orders import (
    PersistedBrokerOrderLink,
    PersistedOrder,
)
from autotrader.persistence.mysql.repositories.fills import MySqlFillStore
from autotrader.shared.time import require_utc


class TradeSource(Protocol):
    async def after(
        self, trade_id: int | None, *, now: datetime
    ) -> tuple[BinanceUsdmTradeFact, ...]:
        """Every fill this account has had since that trade id, oldest first."""
        ...


def binance_broker_order_id(order_id: int) -> str:
    """The identity the order adapter reports, in one place."""
    return f"BINANCE-USDM:{order_id}"


class MySqlLiveFillSettlement:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        account: ExecutionAccount,
        broker_id: UUID,
        trades: TradeSource,
        broker: BrokerSubmitter,
    ) -> None:
        self._sessions = sessions
        self._account = account
        self._broker_id = broker_id
        self._trades = trades
        self._broker = broker
        self._unmatched = 0

    @property
    def unmatched(self) -> int:
        """Fills on this account that belong to no order this system placed."""
        return self._unmatched

    async def settle(self, now: datetime) -> int:
        moment = require_utc(now)
        async with self._sessions() as session:
            cursor = await self._cursor(session)
        facts = await self._trades.after(cursor, now=moment)

        settled: list[tuple[UUID, Decimal]] = []
        for trade in sorted(facts, key=lambda fact: fact.trade_id):
            if cursor is not None and trade.trade_id <= cursor:
                # The source may return the cursor trade itself. Applying it
                # again would be caught by the ledger, but asking is cheaper
                # than being told.
                continue
            async with self._sessions() as session:
                opened = await self._apply(session, trade, moment)
                await session.commit()
            if opened is not None:
                settled.append(opened)

        # The stop is placed against a committed position and dispatched
        # against a committed command, for the same reason the entry is: the
        # broker is reached from a second connection, which cannot see an
        # open transaction.
        for order_id, quantity in settled:
            await self._protect(order_id, quantity, moment)
        return len(facts)

    async def _cursor(self, session: AsyncSession) -> int | None:
        return await session.scalar(
            select(func.max(PersistedFill.source_sequence)).where(
                PersistedFill.account_id == self._account.account.id,
                PersistedFill.source_partition == SOURCE_PARTITION,
            )
        )

    async def _apply(
        self, session: AsyncSession, trade: BinanceUsdmTradeFact, now: datetime
    ) -> tuple[UUID, Decimal] | None:
        """Record one fill, and say whether it opened something to protect."""
        broker_order_id = binance_broker_order_id(trade.order_id)
        link = await session.scalar(
            select(PersistedBrokerOrderLink).where(
                PersistedBrokerOrderLink.broker_order_id == broker_order_id
            )
        )
        if link is None:
            # Traded by hand, or placed by something that is not this system.
            # There is no canonical order to attach it to, and inventing one
            # would put a position in the ledger that no decision authorised.
            self._unmatched += 1
            return None
        order = await session.get(PersistedOrder, link.order_id)
        if order is None:
            raise LookupError("a linked Binance USD-M fill has no canonical order")
        intent = await session.get(PersistedOrderIntent, order.order_intent_id)
        if intent is None:
            raise LookupError("a linked Binance USD-M fill has no intent")
        intent_type = IntentType(intent.intent_type)

        await MySqlFillStore(session).apply_event_once(
            binance_execution_event(
                trade=trade,
                account_id=order.account_id,
                instrument_id=order.instrument_id,
                broker_id=self._broker_id,
                order_id=order.id,
                broker_order_id=broker_order_id,
                broker_client_order_id=order.broker_client_order_id,
                currency=self._account.currency,
                leg_role=LEG_ROLES[intent_type],
                observed_at=now,
            )
        )
        if intent_type is not IntentType.ENTRY:
            return None
        return order.id, trade.quantity

    async def _protect(self, order_id: UUID, quantity: Decimal, now: datetime) -> None:
        async with self._sessions() as session:
            plan = await protection_plan(
                session, order_id=order_id, filled_quantity=quantity
            )
            if plan is None:
                return
            command_id = await create_protective_order(
                session, account=self._account, plan=plan, now=now
            )
            await session.commit()
        if command_id is None:
            return
        async with self._sessions() as session:
            await DispatchService(
                store=MySqlDispatchStore(session, self._account.facts),
                broker=self._broker,
            ).dispatch(command_id=command_id, now=now)
            await session.commit()


__all__ = ("MySqlLiveFillSettlement", "TradeSource", "binance_broker_order_id")
