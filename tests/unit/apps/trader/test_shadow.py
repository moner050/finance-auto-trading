"""What a Shadow loop must not be able to do.

Section 11.7 needs two Shadow sessions before a binding reaches Paper, and a
session completes on decisions rather than orders. So the property worth
holding is not that this loop chooses not to trade - it is that it has no
way to.

The account these run against is LIVE with an order-capable key, which is why
the test is about capability and not about behaviour. A mode that could be
flipped by a stale variable is a mode that eventually is.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid7

import pytest

from autotrader.apps.trader.loop import LoopPorts
from autotrader.apps.trader.shadow import (
    NoBrokerDisagreement,
    NoSettlement,
    NothingToProtect,
    RefusingExecution,
    shadow_ports,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_the_execution_port_cannot_submit() -> None:
    """Not "does not": there is no broker behind it to submit to."""
    execution = RefusingExecution()

    order_id = await execution.submit(
        decision=object(),  # type: ignore[arg-type]
        strategy_decision=object(),
        setup=object(),  # type: ignore[arg-type]
        now=NOW,
    )

    assert order_id is None


@pytest.mark.asyncio
async def test_every_refusal_is_counted() -> None:
    """A session where each tradeable decision was refused only by the
    absence of a broker is a session that would have traded, and that is
    worth being able to see."""
    execution = RefusingExecution()

    for _ in range(3):
        await execution.submit(
            decision=object(),  # type: ignore[arg-type]
            strategy_decision=object(),
            setup=object(),  # type: ignore[arg-type]
            now=NOW,
        )

    assert execution.refused == 3


def test_the_shadow_execution_port_holds_no_broker() -> None:
    """Nothing on it names a venue, a credential or a submitter. A port that
    held one would be one import away from placing an order."""
    execution = RefusingExecution()

    held = {
        name: value
        for name, value in vars(execution).items()
        if not name.startswith("__")
    }

    assert held == {"refused": 0}


@pytest.mark.asyncio
async def test_nothing_settles_reconciles_or_goes_unprotected() -> None:
    """Zero because nothing was ever sent, not because a simulation agreed
    with itself. A paper reconciler here would be comparing two of our own
    tables and calling it agreement with a broker."""
    assert await NoSettlement().settle(NOW) == 0
    assert await NoBrokerDisagreement().reconcile(NOW) == 0
    assert await NothingToProtect().unprotected(NOW) == 0


def test_the_ports_are_wired_with_the_refusing_execution() -> None:
    ports = shadow_ports(
        sessions=object(),  # type: ignore[arg-type]
        source=object(),
        lease=object(),
    )

    assert type(ports) is LoopPorts
    assert type(ports.execution) is RefusingExecution
    assert type(ports.settlement) is NoSettlement
    assert type(ports.reconciliation) is NoBrokerDisagreement
    assert type(ports.protection) is NothingToProtect


def test_the_lease_and_the_control_are_the_real_ones() -> None:
    """An unarmed account must still cost nothing and leave no trace, and two
    instances must still not both believe they are running. Those are not
    relaxed by being in shadow."""
    ports = shadow_ports(
        sessions=object(),  # type: ignore[arg-type]
        source=object(),
        lease=uuid7(),
    )

    assert type(ports.control).__name__ == "MySqlTradingControl"
    assert type(ports.recorder).__name__ == "MySqlDecisionRecorder"
