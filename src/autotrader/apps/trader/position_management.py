"""Deal with what is held before considering anything new.

`manage_v6_position` implements nine actions and nothing in `src/` called it,
so a position, once opened, was never managed: the stop did not move to
break-even, the target was never taken, and a blocking big trade did not get
anyone out. Holding until the initial stop was hit was the only way a trade
ended. This is what calls it.

It runs before the entry evaluation and its answer ends the pass. Closing a
position and opening another against the same bar would be two decisions taken
on one reading of the market, and the second would be sized against exposure
that had just changed underneath it.

The side is the position's, not the one being evaluated for entry. Those can
differ - a short can be held while the divergence has already turned long -
and reading the fibonacci levels or the blocking trade for the wrong direction
would answer a question about a position that is not there.

What actions become is not this module's business. It routes them to a sink,
and the sink is required rather than defaulted: an exit that decided itself
and then quietly submitted nothing is worse than no exit at all, because it
looks wired. Shadow gets one that refuses and counts; anything that actually
trades has to be handed one that actually trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from autotrader.config.settings import RuntimeMode
from autotrader.operations.david_v6_position import (
    V6ManagedPosition,
    V6PositionAction,
    manage_v6_position,
)
from autotrader.strategies.david_v6.costs import FeeSchedule
from autotrader.strategies.david_v6.evidence import V6EvidenceBundle
from autotrader.strategies.david_v6.hlit import HlitFacts
from autotrader.strategies.david_v6.position_facts import position_facts


@dataclass(frozen=True, slots=True)
class PositionMarket:
    """One pass's market, before it is narrowed to a side.

    Narrowing happens inside, because which side to narrow to is a fact about
    the position and the caller has not read it yet.
    """

    bundle: V6EvidenceBundle
    hlit: HlitFacts | None
    current_price: Decimal
    atr_5m: Decimal
    tick_size: Decimal
    fee_schedule: FeeSchedule
    stop_slippage_q95: Decimal | None


class OpenPositions(Protocol):
    async def open_position(
        self, *, account_id: UUID, instrument_id: UUID
    ) -> tuple[UUID, V6ManagedPosition] | None: ...

    async def marks(self, position_id: UUID) -> frozenset[str]: ...

    async def record_mark(
        self, *, position_id: UUID, mark: str, now: datetime
    ) -> None: ...


class PositionActions(Protocol):
    """Whatever turns a decided action into an effect."""

    async def apply(
        self,
        action: V6PositionAction,
        *,
        position: V6ManagedPosition,
        position_id: UUID,
        now: datetime,
    ) -> None: ...


class ProtectionHealth(Protocol):
    async def failed(self, *, account_id: UUID, instrument_id: UUID) -> bool: ...


class RefusingPositionActions:
    """A sink with nothing behind it, for a mode that places no orders.

    Telemetry still lands, because recording that price reached a level is an
    observation rather than an order. Everything else is counted and refused,
    and the count is worth keeping: a session where an exit was decided and
    could not be taken is a session that would have exited.
    """

    def __init__(self, positions: OpenPositions) -> None:
        self._positions = positions
        self.recorded = 0
        self.refused = 0

    async def apply(
        self,
        action: V6PositionAction,
        *,
        position: V6ManagedPosition,
        position_id: UUID,
        now: datetime,
    ) -> None:
        del position
        if action.telemetry_only:
            await self._positions.record_mark(
                position_id=position_id, mark=action.kind.value, now=now
            )
            self.recorded += 1
            return
        self.refused += 1


class V6PositionManager:
    """Read the position, decide what it needs, and route the answer."""

    def __init__(
        self,
        *,
        positions: OpenPositions,
        actions: PositionActions,
        protection: ProtectionHealth,
        account_id: UUID,
        instrument_id: UUID,
        mode: RuntimeMode,
    ) -> None:
        self._positions = positions
        self._actions = actions
        self._protection = protection
        self._account_id = account_id
        self._instrument_id = instrument_id
        self._mode = mode
        self.managed = 0

    async def manage(self, market: PositionMarket, *, now: datetime) -> bool:
        """Whether anything was done, which ends the pass when it was."""
        held = await self._positions.open_position(
            account_id=self._account_id, instrument_id=self._instrument_id
        )
        if held is None:
            return False
        position_id, position = held
        facts = position_facts(
            market.bundle,
            side=position.side,
            setup=None if market.hlit is None else market.hlit.for_side(position.side),
            current_price=market.current_price,
            atr_5m=market.atr_5m,
            tick_size=market.tick_size,
            fee_schedule=market.fee_schedule,
            stop_slippage_q95=market.stop_slippage_q95,
            protection_failed=await self._protection.failed(
                account_id=self._account_id, instrument_id=self._instrument_id
            ),
        )
        actions = manage_v6_position(position, facts, mode=self._mode)
        for action in actions:
            await self._actions.apply(
                action, position=position, position_id=position_id, now=now
            )
        self.managed += len(actions)
        return bool(actions)


__all__ = (
    "OpenPositions",
    "PositionActions",
    "PositionMarket",
    "ProtectionHealth",
    "RefusingPositionActions",
    "V6PositionManager",
)
