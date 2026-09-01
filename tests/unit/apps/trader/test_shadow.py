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
from types import SimpleNamespace
from uuid import uuid7

import pytest

from autotrader.apps.trader.loop import LoopPorts
from autotrader.apps.trader.shadow import (
    NoBrokerDisagreement,
    NoSettlement,
    NothingToProtect,
    RefusingExecution,
    ShadowTradingControl,
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


def test_the_recorder_is_the_real_one_and_the_control_is_not() -> None:
    """Decisions are written the same way they always are - the whole point
    is evidence. The control is the one thing shadow relaxes, and it relaxes
    exactly one half of it: see the tests below."""
    ports = shadow_ports(
        sessions=object(),  # type: ignore[arg-type]
        source=object(),
        lease=uuid7(),
    )

    assert type(ports.recorder).__name__ == "MySqlDecisionRecorder"
    assert type(ports.control) is ShadowTradingControl


class _Controls:
    """Stands in for the stored trading control rows."""

    def __init__(self, *levels: str) -> None:
        self.rows = tuple(
            SimpleNamespace(armed=False, kill_switch_level=level) for level in levels
        )

    def __call__(self) -> _Controls:
        return self

    async def __aenter__(self) -> _Controls:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def scalars(self, _statement: object) -> _Controls:
        return self

    def all(self) -> tuple[object, ...]:
        return self.rows


@pytest.mark.asyncio
async def test_shadow_runs_when_nobody_has_intervened() -> None:
    """No control rows means nobody armed anything, and for a loop that takes
    no exposure that is not a reason to stop. `MySqlTradingControl` answers no
    to a different question."""
    control = ShadowTradingControl(_Controls())  # type: ignore[arg-type]

    assert await control.is_armed() is True


@pytest.mark.asyncio
async def test_shadow_runs_without_the_account_being_armed() -> None:
    """Requiring ARMED would mean arming a LIVE account to collect the
    evidence that is supposed to come before it is armed."""
    control = ShadowTradingControl(_Controls("NONE", "NONE"))  # type: ignore[arg-type]

    assert await control.is_armed() is True
    assert all(row.armed is False for row in control._sessions.rows)  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ("BLOCK_NEW_EXPOSURE", "EMERGENCY"))
async def test_any_kill_switch_stops_the_shadow_loop(level: str) -> None:
    """BLOCK_NEW_EXPOSURE names something Shadow never does, so continuing
    could be argued. It stops anyway: a level above NONE means somebody
    intervened, and evidence gathered during an intervention is not evidence
    of how the strategy behaves."""
    control = ShadowTradingControl(_Controls("NONE", level))  # type: ignore[arg-type]

    assert await control.is_armed() is False
