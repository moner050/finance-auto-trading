"""The driver that turns one tick into a running loop.

A pass is the unit worth reasoning about: hold the lease, settle whatever the
last pass left open, then evaluate if a new bar has closed. The scheduling
around it is deliberately a few lines, because a loop that is hard to read is
a loop nobody trusts to leave running.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from autotrader.apps.trader.tick import (
    DecisionRecorder,
    Execution,
    PositionManagement,
    TickContext,
    TickOutcome,
    TradingControl,
    run_tick,
)
from autotrader.shared.time import require_utc

NOT_LEADER = "NOT_LEADER"
NO_NEW_BAR = "NO_NEW_BAR"
UNPROTECTED = "UNPROTECTED"
UNRECONCILED = "UNRECONCILED"


class SchedulerLease(Protocol):
    async def acquire(self, now: datetime) -> bool:
        """Whether this instance holds the right to run."""
        ...


class FillSettlement(Protocol):
    async def settle(self, now: datetime) -> int:
        """Resolve orders whose fill bar has closed, returning how many."""
        ...


class Reconciler(Protocol):
    async def reconcile(self, now: datetime) -> int:
        """How many ways the broker disagrees about this account."""
        ...


class ProtectionGuard(Protocol):
    async def unprotected(self, now: datetime) -> int:
        """How many open positions have no stop standing behind them."""
        ...


class ContextSource(Protocol):
    async def context_for(self, now: datetime) -> TickContext | None:
        """The next evaluation, or None when no new bar has closed."""
        ...


class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True, slots=True)
class LoopPass:
    reason: str
    settled: int
    outcome: TickOutcome | None


@dataclass(frozen=True, slots=True)
class LoopPorts:
    lease: SchedulerLease
    settlement: FillSettlement
    reconciliation: Reconciler
    protection: ProtectionGuard
    source: ContextSource
    control: TradingControl
    recorder: DecisionRecorder
    execution: Execution
    # None means no position can exist in this mode. Shadow places nothing, so
    # there is nothing held to manage; anything that trades is handed one.
    position: PositionManagement | None = None


async def run_pass(*, now: datetime, ports: LoopPorts) -> LoopPass:
    moment = require_utc(now)
    if type(cast(object, ports)) is not LoopPorts:
        raise TypeError("ports must be exact LoopPorts")
    # Losing the lease means another instance owns this account. Doing nothing
    # is the whole point: two loops trading one account is the worst outcome.
    if not await ports.lease.acquire(moment):
        return LoopPass(reason=NOT_LEADER, settled=0, outcome=None)

    # Settle first so the evaluation sees positions as they actually stand.
    settled = await ports.settlement.settle(moment)

    # Ask the broker what it holds before trusting our own answer. Checking
    # protection first would check it against numbers that may be wrong, and
    # a stop sized from a position we do not really have protects nothing.
    if await ports.reconciliation.reconcile(moment):
        return LoopPass(reason=UNRECONCILED, settled=settled, outcome=None)

    # Then check what is already held before deciding anything new. An
    # unprotected position is not a reason to trade more carefully; it is a
    # reason to stop opening exposure until it has a stop behind it.
    if await ports.protection.unprotected(moment):
        return LoopPass(reason=UNPROTECTED, settled=settled, outcome=None)

    context = await ports.source.context_for(moment)
    if context is None:
        return LoopPass(reason=NO_NEW_BAR, settled=settled, outcome=None)
    outcome = await run_tick(
        context,
        control=ports.control,
        recorder=ports.recorder,
        execution=ports.execution,
        position=ports.position,
    )
    return LoopPass(reason=outcome.reason, settled=settled, outcome=outcome)


async def run_forever(
    *,
    ports: LoopPorts,
    clock: Clock,
    interval: timedelta,
    stop: asyncio.Event,
) -> None:
    """Run passes until asked to stop, one at a time."""
    if type(interval) is not timedelta or interval <= timedelta(0):
        raise ValueError("interval must be positive")
    seconds = interval.total_seconds()
    while not stop.is_set():
        await run_pass(now=clock.now(), ports=ports)
        if stop.is_set():
            return
        await clock.sleep(seconds)


class SystemClock:
    def now(self) -> datetime:
        """Truncated to a whole second.

        The stored columns keep microseconds, so this is no longer needed to
        survive a round trip. It stays because a pass is a bar boundary: two
        readings inside one second describe the same tick, and giving them
        different timestamps would invent an ordering the market did not have.
        """
        return datetime.now(UTC).replace(microsecond=0)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


__all__ = (
    "NOT_LEADER",
    "NO_NEW_BAR",
    "UNPROTECTED",
    "UNRECONCILED",
    "Clock",
    "ContextSource",
    "FillSettlement",
    "LoopPass",
    "LoopPorts",
    "ProtectionGuard",
    "Reconciler",
    "SchedulerLease",
    "SystemClock",
    "run_forever",
    "run_pass",
)
