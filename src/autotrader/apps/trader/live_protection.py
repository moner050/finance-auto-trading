"""The emergency close, and the reader that names it.

`BinanceUsdmProtectionService` closes a position when it cannot put a stop
behind it. That is what the protection deadline is for, and it needs two
things nothing was providing.

**A command already prepared.** `prepare_full_close` must return the command
whose id the `EntryFill` already named, so the close has to exist before
protection is attempted rather than being invented when it fails. A close
decided at the moment protection breaks is one that has to succeed on its
first try through a path nothing has exercised; a close prepared in advance
only has to be dispatched.

**Somewhere to read the fill from.** `ProtectionContext` was left as a named
boundary in the live submitter, because a `BrokerOrderCommand` carries no tick
size, no fill price and no deadline. This is the reader, and it refuses rather
than approximates: every field of an `EntryFill` decides something - the side
the stop exits on, the price it may not sit through, the tick it rounds to -
and a guess at any of them is a stop placed somewhere nothing chose.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.trader.composition import (
    ExecutionAccount,
    ProtectionPlan,
    create_protective_order,
)
from autotrader.apps.trader.quotes import QuoteSource
from autotrader.domain.enums import IntentType, Side
from autotrader.execution.orders.models import BrokerOrderCommand
from autotrader.execution.reconciliation.service import BrokerSnapshotReader
from autotrader.integrations.brokers.binance_usdm.algo_orders import EntryFill
from autotrader.integrations.brokers.binance_usdm.live_submitter import (
    ProtectionPlacement,
)
from autotrader.integrations.brokers.binance_usdm.orders import BrokerWriteResult
from autotrader.persistence.mysql.dispatch_store import MySqlDispatchStore
from autotrader.persistence.mysql.models.binance_usdm import (
    BinanceUsdmAlgoOrderRow,
)
from autotrader.persistence.mysql.models.bindings import ProviderAccountBinding
from autotrader.persistence.mysql.models.fills import PersistedFill
from autotrader.persistence.mysql.models.intents import PersistedOrderIntent
from autotrader.persistence.mysql.models.orders import (
    PersistedOrder,
    PersistedOrderCommand,
)
from autotrader.persistence.mysql.models.positions import Position
from autotrader.risk.v6 import ProtectionAuthority
from autotrader.shared.time import require_utc

EMERGENCY_CLOSE = "EMERGENCY_CLOSE"

# How long a position may be unprotected before it is closed instead. §22.7
# names no number. This is short because the alternative to closing is holding
# something whose loss has no floor, and long enough that one retry of a
# placement fits inside it.
PROTECTION_DEADLINE = timedelta(seconds=30)


class ProtectionContextUnavailable(RuntimeError):
    """The fill behind a protective command cannot be read."""


async def create_emergency_close_order(
    session: AsyncSession,
    *,
    account: ExecutionAccount,
    plan: ProtectionPlan,
    quotes: QuoteSource,
    now: datetime,
) -> UUID | None:
    """Prepare the close that runs if protection cannot be established.

    Through `create_protective_order`, which keeps one answer to what a
    closing order is. The reason is what keeps the two apart: it is part of
    the intent's identity, so this does not take the intent belonging to the
    stop it exists in case of.
    """
    return await create_protective_order(
        session,
        account=account,
        plan=plan,
        now=now,
        reason_code=EMERGENCY_CLOSE,
        intent_type=IntentType.EXIT,
        # It goes to the market rather than waiting at a price, so it carries
        # no trigger and needs the price it will get. §31.11.
        trigger_price=None,
        quote=await quotes.quote(),
        # `_validate_emergency_command` requires this exactly, and it is right
        # to: a close that could rest unfilled is not a close.
        time_in_force="NONE",
    )


class NormalOrders:
    """The part of the order service an emergency close reuses unchanged."""

    async def submit_locked(self, command: BrokerOrderCommand) -> BrokerWriteResult: ...

    async def recover_by_client_id(self, client_order_id: str) -> BrokerWriteResult: ...


@dataclass(frozen=True, slots=True)
class MySqlEmergencyOrders:
    """`BinanceUsdmEmergencyOrderService` over the loop's own tables.

    Submitting and recovering are the ordinary order path's, unchanged: an
    emergency close is a market order that happens to be urgent. What is here
    is the part that is not - finding the command prepared for this fill, and
    asking the venue whether the position is actually gone.
    """

    sessions: async_sessionmaker[AsyncSession]
    orders: NormalOrders
    snapshots: BrokerSnapshotReader
    account_id: UUID
    instrument_id: UUID
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def prepare_full_close(self, fill: EntryFill) -> BrokerOrderCommand:
        try:
            async with self.sessions() as session:
                return await MySqlDispatchStore(session).command_for_recovery(
                    command_id=fill.emergency_close_command_id
                )
        except ValueError:
            # Nothing to fall back to means the position cannot be closed by
            # the path that exists for exactly this. Saying so is the only
            # honest answer available here, and it says it as the protection
            # service's own failure rather than as a lookup that went wrong.
            raise ProtectionContextUnavailable(
                "the emergency close for this fill was never prepared"
            ) from None

    async def submit_locked(self, command: BrokerOrderCommand) -> BrokerWriteResult:
        return await self.orders.submit_locked(command)

    async def recover_by_client_id(self, client_order_id: str) -> BrokerWriteResult:
        return await self.orders.recover_by_client_id(client_order_id)

    async def confirm_zero_position(self, fill: EntryFill) -> bool:
        """Whether the venue agrees the position is gone.

        The venue rather than the ledger. The ledger learns of a close when
        settlement reads it back, which is after this is asked, so answering
        from the ledger would answer about the moment before.

        A flat instrument is absent from a snapshot rather than reported at
        zero, which is what makes this readable as absence.
        """
        del fill  # One reader, one account, one instrument.
        snapshot = await self.snapshots.read_snapshot(
            account_id=self.account_id, now=require_utc(self.clock())
        )
        if not snapshot.complete:
            # A partial snapshot cannot prove absence: the instrument may be
            # in the part that did not arrive.
            return False
        return all(
            held.instrument_id != self.instrument_id for held in snapshot.positions
        )


@dataclass(frozen=True, slots=True)
class MySqlProtectionContext:
    """What a protective command does not carry, read from what does."""

    sessions: async_sessionmaker[AsyncSession]
    account: ExecutionAccount
    tick_size: Decimal
    symbol: str = "BTCUSDT"
    deadline: timedelta = PROTECTION_DEADLINE

    async def placement_for(self, command: BrokerOrderCommand) -> ProtectionPlacement:
        if command.trigger_price is None:
            raise ProtectionContextUnavailable(
                "a command with no trigger price is not a protective stop"
            )
        async with self.sessions() as session:
            position = await self._position_for(session, command)
            fill = await self._entry_fill(session, position)
            superseded = await self._working_stop(session, fill.entry_command_id)
        return ProtectionPlacement(
            fill=fill,
            # The order reserved risk before it was created, so its terms are
            # an approval rather than a claim. §31.9.
            authority=ProtectionAuthority.approved(
                stop_price=command.trigger_price, quantity=command.quantity
            ),
            superseded_client_algo_id=superseded,
        )

    async def _position_for(
        self, session: AsyncSession, command: BrokerOrderCommand
    ) -> Position:
        order = await session.get(PersistedOrder, command.order_id)
        if order is None:
            raise ProtectionContextUnavailable(
                "a protective command has no canonical order"
            )
        intent = await session.get(PersistedOrderIntent, order.order_intent_id)
        if intent is None or intent.protection_position_id is None:
            raise ProtectionContextUnavailable("a protective command names no position")
        position = await session.get(Position, intent.protection_position_id)
        if position is None:
            raise ProtectionContextUnavailable("the position a stop protects is gone")
        return position

    async def _entry_fill(self, session: AsyncSession, position: Position) -> EntryFill:
        """The fill that opened this position, as the algo path reads it.

        The earliest fill of the latest entry order, rather than the earliest
        fill outright: one instrument is entered more than once over a day,
        and the stop belongs to the position standing now.
        """
        entry_order = await session.scalar(
            select(PersistedOrder)
            .join(
                PersistedOrderIntent,
                PersistedOrderIntent.id == PersistedOrder.order_intent_id,
            )
            .where(
                PersistedOrder.account_id == position.account_id,
                PersistedOrder.instrument_id == position.instrument_id,
                PersistedOrderIntent.intent_type == IntentType.ENTRY.value,
                PersistedOrder.filled_quantity > 0,
            )
            .order_by(PersistedOrder.created_at.desc())
            .limit(1)
        )
        if entry_order is None:
            raise ProtectionContextUnavailable(
                "the position behind this stop has no filled entry"
            )
        fill = await session.scalar(
            select(PersistedFill)
            .where(PersistedFill.order_id == entry_order.id)
            .order_by(PersistedFill.executed_at, PersistedFill.source_sequence)
            .limit(1)
        )
        if fill is None:
            raise ProtectionContextUnavailable(
                "the entry behind this stop recorded no fill"
            )
        command = await _command_of(session, entry_order.id)
        if command is None:
            raise ProtectionContextUnavailable(
                "the entry behind this stop has no dispatched command"
            )
        emergency = await self._emergency_command(session, position)
        filled_at = require_utc(fill.executed_at)
        return EntryFill(
            entry_command_id=command,
            account_id=position.account_id,
            instrument_id=position.instrument_id,
            binding_id=await self._binding(session),
            side=Side(entry_order.side),
            first_fill_quantity=fill.quantity,
            # The first fill of the entry, so nothing preceded it.
            cumulative_quantity_before=Decimal(0),
            average_fill_price=fill.price,
            symbol=self.symbol,
            tick_size=self.tick_size,
            filled_at=filled_at,
            protection_deadline=filled_at + self.deadline,
            emergency_close_command_id=emergency,
        )

    async def _emergency_command(
        self, session: AsyncSession, position: Position
    ) -> UUID:
        order = await session.scalar(
            select(PersistedOrder)
            .join(
                PersistedOrderIntent,
                PersistedOrderIntent.id == PersistedOrder.order_intent_id,
            )
            .where(
                PersistedOrderIntent.protection_position_id == position.id,
                PersistedOrderIntent.protection_reason_code == EMERGENCY_CLOSE,
            )
            .order_by(PersistedOrder.created_at.desc())
            .limit(1)
        )
        if order is None:
            raise ProtectionContextUnavailable(
                "no emergency close was prepared for this position"
            )
        command = await _command_of(session, order.id)
        if command is None:
            raise ProtectionContextUnavailable(
                "the emergency close for this position has no command"
            )
        return command

    async def _working_stop(
        self, session: AsyncSession, entry_command_id: UUID
    ) -> str | None:
        """The stop this one replaces, if one is working.

        Read rather than inferred from the command type: a REPLACE whose
        predecessor is already gone is a first placement, and a SUBMIT issued
        while a stop is working is a move. §31.9.

        Only an ACTIVE record counts. Anything else is protection nobody has
        established, and superseding it would place a second stop behind a
        position whose first one may or may not exist.
        """
        return await session.scalar(
            select(BinanceUsdmAlgoOrderRow.client_algo_id)
            .where(
                BinanceUsdmAlgoOrderRow.entry_command_id == entry_command_id,
                BinanceUsdmAlgoOrderRow.state == "ACTIVE",
            )
            .order_by(BinanceUsdmAlgoOrderRow.prepared_at.desc())
            .limit(1)
        )

    async def _binding(self, session: AsyncSession) -> UUID:
        """The provider binding this account trades Binance USD-M under.

        `ExecutionAccount` does not carry it - it is about the account and the
        policy bound to it, not about which provider registration is live - so
        it is read, and read as the active one rather than the newest.
        """
        binding = await session.scalar(
            select(ProviderAccountBinding.id)
            .where(
                ProviderAccountBinding.account_id == self.account.account.id,
                ProviderAccountBinding.provider_code == "BINANCE",
                ProviderAccountBinding.active.is_(True),
            )
            .order_by(ProviderAccountBinding.revision.desc())
            .limit(1)
        )
        if binding is None:
            raise ProtectionContextUnavailable(
                "this account has no active Binance USD-M binding"
            )
        return binding


async def _command_of(session: AsyncSession, order_id: UUID) -> UUID | None:
    """The command an order was dispatched under, read the way it was written."""
    return await session.scalar(
        select(PersistedOrderCommand.id).where(
            PersistedOrderCommand.order_id == order_id
        )
    )


__all__ = (
    "EMERGENCY_CLOSE",
    "PROTECTION_DEADLINE",
    "MySqlEmergencyOrders",
    "MySqlProtectionContext",
    "NormalOrders",
    "ProtectionContextUnavailable",
    "create_emergency_close_order",
)
