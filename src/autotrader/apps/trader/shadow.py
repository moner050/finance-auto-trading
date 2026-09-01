"""The loop that watches and writes down what it would have done.

Section 11.7 requires two Shadow sessions on distinct dates before a binding
can go to Paper, and a session only completes with decisions recorded against
it. So the first loop that needs to exist is one that evaluates real bars and
records real decisions and places nothing.

Shadow is not a flag read at the last moment. `RefusingExecution` is the
execution port, and it has no way to submit: there is no broker behind it, no
credentials reach it, and its `submit` returns None whatever it is handed.
A mode that could be flipped by a stale variable is a mode that eventually is;
this one is the absence of the capability.

The rest of the ports fall out of that. Nothing is ever submitted, so nothing
settles and nothing needs reconciling against a broker, and both are the
honest zero rather than a paper simulation of orders that were never placed.
The lease and the trading control are real: an unarmed account still costs
nothing and still leaves no trace, and two instances still must not both
believe they are running.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.trader.composition import (
    MySqlDecisionRecorder,
    MySqlTradingControl,
)
from autotrader.apps.trader.loop import LoopPorts
from autotrader.strategies.david_v6.hlit import HlitSetup
from autotrader.strategies.david_v6.models import V6Decision

SHADOW = "SHADOW"


class RefusingExecution:
    """An execution port with nothing behind it.

    Returning None is what `run_tick` reads as "not tradeable after all", so a
    shadow pass records its decision and stops there. The count of these is
    worth keeping: a session where every tradeable decision was refused by the
    absence of a broker is a session that would have traded.
    """

    def __init__(self) -> None:
        self.refused = 0

    async def submit(
        self,
        *,
        decision: V6Decision,
        strategy_decision: object,
        setup: HlitSetup,
        now: datetime,
    ) -> UUID | None:
        del decision, strategy_decision, setup, now
        self.refused += 1
        return None


class NoSettlement:
    """Nothing was submitted, so nothing can have filled."""

    async def settle(self, now: datetime) -> int:
        del now
        return 0


class NoBrokerDisagreement:
    """Reconciliation compares this system against a broker it never told.

    Zero here is a fact rather than a stub: with no order ever sent there is
    no position at the venue this loop could disagree with. A paper reconciler
    would instead be comparing two of our own tables and calling it agreement.
    """

    async def reconcile(self, now: datetime) -> int:
        del now
        return 0


class NothingToProtect:
    """A stop stands behind every position, and there are no positions."""

    async def unprotected(self, now: datetime) -> int:
        del now
        return 0


def shadow_ports(
    *,
    sessions: async_sessionmaker[AsyncSession],
    source: object,
    lease: object,
    execution: RefusingExecution | None = None,
) -> LoopPorts:
    """One loop that can evaluate and record, and cannot place an order."""
    return LoopPorts(
        lease=lease,  # type: ignore[arg-type]
        settlement=NoSettlement(),
        reconciliation=NoBrokerDisagreement(),
        protection=NothingToProtect(),
        source=source,  # type: ignore[arg-type]
        control=MySqlTradingControl(sessions),
        recorder=MySqlDecisionRecorder(sessions),
        execution=execution or RefusingExecution(),
    )


__all__ = (
    "SHADOW",
    "NoBrokerDisagreement",
    "NoSettlement",
    "NothingToProtect",
    "RefusingExecution",
    "shadow_ports",
)
