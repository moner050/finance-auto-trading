"""Rebuild a managed position from what the account already recorded.

`manage_v6_position` decides what to do with an open position, and it wants a
`V6ManagedPosition` describing one. Nothing was calling it, so nothing had ever
had to answer where that description comes from.

Almost all of it is already stored, in the tables the entry left behind:

    position.quantity, position.average_cost         what is held now
    lots                                             how many adds, and the first
    first lot -> opening fill                        the initial entry price
    order -> intent -> signal -> v6 decision         the structural stop
    live protective intent                           the stop actually working

That chain is not invented here. `_protection_plan` in the trader composition
already walks the same one to find the stop when an entry fills, which is what
says it holds.

Four things are not derivable, and they are the four the manager carries as
flags: whether the fibonacci records and the Shadow partial observations have
already been emitted. "Have we said this already" is not a fact about the
market, so it is stored, one row per observation per position.

What this deliberately does not do is guess. A position whose decision cannot
be found has no structural stop to rebuild from, and inventing one would put a
risk limit on record that the strategy never agreed to - the same refusal
`_protection_plan` already makes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.domain.enums import IntentType, Side
from autotrader.operations.david_v6_position import (
    V6ManagedPosition,
    V6PositionActionKind,
)
from autotrader.persistence.mysql.models.david_v6 import (
    DavidV6DecisionRow,
    DavidV6PositionMarkRow,
)
from autotrader.persistence.mysql.models.fills import PersistedFill
from autotrader.persistence.mysql.models.intents import PersistedOrderIntent
from autotrader.persistence.mysql.models.orders import PersistedOrder
from autotrader.persistence.mysql.models.positions import PersistedPositionLot, Position
from autotrader.shared.time import require_utc

# The marks whose only purpose is to stop a repeat. Kept as the action name so
# a reader of the table sees the action, not a private code.
FIB_25 = V6PositionActionKind.RECORD_FIB_25.value
FIB_50 = V6PositionActionKind.RECORD_FIB_50_RESEARCH.value
PARTIAL_1_2R = V6PositionActionKind.OBSERVE_PARTIAL_1_2R.value
PARTIAL_1_5R = V6PositionActionKind.OBSERVE_PARTIAL_1_5R.value


class ManagedPositionUnavailableError(RuntimeError):
    """Raised when the position cannot be described without guessing."""


class MySqlManagedPositions:
    """Read one open position back as the manager wants to see it."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def marks(self, position_id: UUID) -> frozenset[str]:
        rows = await self._session.scalars(
            select(DavidV6PositionMarkRow.mark).where(
                DavidV6PositionMarkRow.position_id == position_id
            )
        )
        return frozenset(rows.all())

    async def record_mark(self, *, position_id: UUID, mark: str, now: datetime) -> None:
        """Store an observation as emitted. A repeat collides on the key."""
        self._session.add(
            DavidV6PositionMarkRow(
                position_id=position_id,
                mark=mark,
                recorded_at=require_utc(now),
            )
        )
        await self._session.flush()

    async def open_position(
        self, *, account_id: UUID, instrument_id: UUID
    ) -> tuple[UUID, V6ManagedPosition] | None:
        """The open position and its id, or None when there is nothing held."""
        position = await self._session.scalar(
            select(Position).where(
                Position.account_id == account_id,
                Position.instrument_id == instrument_id,
            )
        )
        if position is None or position.quantity <= 0:
            return None

        entries = await self._entries(position.id)
        if not entries:
            raise ManagedPositionUnavailableError(
                "an open position has no entry to read its opening from"
            )
        first_order_id, first_side = entries[0][0], entries[0][1]
        decision = await self._decision_for(first_order_id)
        active_stop = await self._active_stop(
            account_id=account_id, instrument_id=instrument_id
        )
        return position.id, managed_position_from(
            side=first_side,
            entries=tuple(entry[2] for entry in entries),
            average_cost=position.average_cost,
            remaining_quantity=position.quantity,
            structural_stop=decision.structural_stop,
            active_stop=active_stop,
            marks=await self.marks(position.id),
        )

    async def _entries(
        self, position_id: UUID
    ) -> tuple[tuple[UUID, Side, OpeningEntry], ...]:
        """The entry orders behind this position, oldest first.

        Grouped by order because a lot is per fill: one entry filling in three
        pieces leaves three lots and no adds at all.
        """
        rows = (
            await self._session.execute(
                select(
                    PersistedFill.order_id,
                    PersistedFill.side,
                    PersistedFill.quantity,
                    PersistedFill.price,
                    PersistedFill.executed_at,
                )
                .join(
                    PersistedPositionLot,
                    PersistedPositionLot.opening_fill_id == PersistedFill.id,
                )
                .where(PersistedPositionLot.position_id == position_id)
                .order_by(PersistedFill.executed_at, PersistedFill.id)
            )
        ).all()
        by_order: dict[UUID, list[tuple[Decimal, Decimal]]] = {}
        sides: dict[UUID, Side] = {}
        for order_id, side, quantity, price, _ in rows:
            by_order.setdefault(order_id, []).append((quantity, price))
            sides.setdefault(order_id, Side(side))
        entries: list[tuple[UUID, Side, OpeningEntry]] = []
        for order_id, fills in by_order.items():
            total = sum((quantity for quantity, _ in fills), start=Decimal(0))
            if total <= 0:
                continue
            # Volume weighted, because a partial fill at a worse price is part
            # of what this entry actually cost.
            notional = sum(
                (quantity * price for quantity, price in fills), start=Decimal(0)
            )
            entries.append(
                (
                    order_id,
                    sides[order_id],
                    OpeningEntry(quantity=total, average_price=notional / total),
                )
            )
        return tuple(entries)

    async def _decision_for(self, order_id: UUID) -> _DecisionFacts:
        order = await self._session.get(PersistedOrder, order_id)
        if order is None:
            raise ManagedPositionUnavailableError("an opening fill has no order")
        intent = await self._session.get(PersistedOrderIntent, order.order_intent_id)
        if intent is None:
            raise ManagedPositionUnavailableError("an opening order has no intent")
        if intent.strategy_signal_id is None:
            raise ManagedPositionUnavailableError(
                "an opening entry has no strategy signal to read a stop from"
            )
        row = (
            await self._session.execute(
                select(
                    DavidV6DecisionRow.structural_stop,
                    DavidV6DecisionRow.target_price,
                ).where(
                    DavidV6DecisionRow.strategy_signal_id == intent.strategy_signal_id
                )
            )
        ).first()
        if row is None or row[0] is None:
            # The same refusal the protective-order path already makes: a
            # position whose decision named no stop has no risk limit to
            # rebuild, and inventing one puts a number on record that the
            # strategy never agreed to.
            raise ManagedPositionUnavailableError(
                "the decision that opened this position recorded no stop"
            )
        return _DecisionFacts(structural_stop=row[0], target_price=row[1])

    async def _active_stop(
        self, *, account_id: UUID, instrument_id: UUID
    ) -> Decimal | None:
        """The trigger of the protective order that is actually working."""
        return await self._session.scalar(
            select(PersistedOrder.trigger_price)
            .join(
                PersistedOrderIntent,
                PersistedOrderIntent.id == PersistedOrder.order_intent_id,
            )
            .where(
                PersistedOrder.account_id == account_id,
                PersistedOrder.instrument_id == instrument_id,
                PersistedOrderIntent.intent_type == IntentType.PROTECTIVE.value,
                PersistedOrder.trigger_price.is_not(None),
            )
            .order_by(PersistedOrder.created_at.desc())
            .limit(1)
        )


class _DecisionFacts:
    __slots__ = ("structural_stop", "target_price")

    def __init__(
        self, *, structural_stop: Decimal, target_price: Decimal | None
    ) -> None:
        self.structural_stop = structural_stop
        self.target_price = target_price


@dataclass(frozen=True, slots=True)
class OpeningEntry:
    """One entry order that opened or added to this position.

    An order rather than a lot, and the distinction is the whole point. A lot
    is written per fill, so one entry that filled in three pieces leaves three
    lots - and counting lots as adds would report two adds where the operator
    placed none. `V6ManagedPosition` allows at most one add and refuses the
    rest, so that miscount does not size a trade wrongly; it raises, and the
    position becomes unmanageable.
    """

    quantity: Decimal
    average_price: Decimal


def managed_position_from(
    *,
    side: Side,
    entries: tuple[OpeningEntry, ...],
    average_cost: Decimal,
    remaining_quantity: Decimal,
    structural_stop: Decimal,
    active_stop: Decimal | None,
    marks: frozenset[str],
) -> V6ManagedPosition:
    """Assemble the description, with no database in reach.

    Separate from the reading so the arithmetic can be stated against values
    rather than against a fixture of six joined tables.
    """
    if not entries:
        raise ManagedPositionUnavailableError(
            "an open position has no entry to read its opening from"
        )
    opening = entries[0]
    stop_for_risk = active_stop if active_stop is not None else structural_stop
    return V6ManagedPosition(
        side=side,
        initial_entry_price=opening.average_price,
        average_entry_price=average_cost,
        # The size the position opened with, not what is left of it. Reading
        # this from the remainder would shrink R every time a part closed.
        initial_quantity=opening.quantity,
        remaining_quantity=remaining_quantity,
        initial_stop_price=structural_stop,
        active_stop_price=active_stop,
        # Working, not merely intended. The manager's first act is to put the
        # initial stop on when nothing is behind the position.
        initial_stop_active=active_stop is not None,
        original_approved_risk=opening.quantity
        * abs(opening.average_price - structural_stop),
        current_worst_case_risk=remaining_quantity * abs(average_cost - stop_for_risk),
        add_count=len(entries) - 1,
        break_even_active=_at_break_even(
            side=side, average=average_cost, stop=active_stop
        ),
        fib_25_recorded=FIB_25 in marks,
        fib_50_recorded=FIB_50 in marks,
        shadow_1_2r_recorded=PARTIAL_1_2R in marks,
        shadow_1_5r_recorded=PARTIAL_1_5R in marks,
    )


def _at_break_even(*, side: Side, average: Decimal, stop: Decimal | None) -> bool:
    """Whether the working stop is already at or beyond the average entry.

    Read from where the stop sits rather than stored, so a stop moved by hand
    at the venue is reflected rather than contradicted.
    """
    if stop is None:
        return False
    return stop >= average if side is Side.BUY else stop <= average


__all__ = (
    "FIB_25",
    "FIB_50",
    "PARTIAL_1_2R",
    "PARTIAL_1_5R",
    "ManagedPositionUnavailableError",
    "MySqlManagedPositions",
    "OpeningEntry",
    "managed_position_from",
)
