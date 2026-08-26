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
    TickContext,
    TickOutcome,
    TradingControl,
    run_tick,
)
from autotrader.shared.time import require_utc

NOT_LEADER = "NOT_LEADER"
NO_NEW_BAR = "NO_NEW_BAR"


class SchedulerLease(Protocol):
    async def acquire(self, now: datetime) -> bool:
        """Whether this instance holds the right to run."""
        ...


class FillSettlement(Protocol):
    async def settle(self, now: datetime) -> int:
        """Resolve orders whose fill bar has closed, returning how many."""
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
    source: ContextSource
    control: TradingControl
    recorder: DecisionRecorder
    execution: Execution


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

    context = await ports.source.context_for(moment)
    if context is None:
        return LoopPass(reason=NO_NEW_BAR, settled=settled, outcome=None)
    outcome = await run_tick(
        context,
        control=ports.control,
        recorder=ports.recorder,
        execution=ports.execution,
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
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


__all__ = (
    "NOT_LEADER",
    "NO_NEW_BAR",
    "Clock",
    "ContextSource",
    "FillSettlement",
    "LoopPass",
    "LoopPorts",
    "SchedulerLease",
    "SystemClock",
    "run_forever",
    "run_pass",
)
