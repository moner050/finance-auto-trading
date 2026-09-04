"""What happens to the controls when a position cannot be protected.

The property under all of these: the kill switch only ever moves upward. A
safety action that could weaken one would be able to undo a halt somebody
else raised, which is the opposite of what it is for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid7

import pytest

from autotrader.apps.trader.live_safety import MySqlProtectionSafetyActions

BINDING = uuid7()


@dataclass
class _Control:
    kill_switch_level: str = "NONE"
    armed: bool = True
    row_version: int = 1


@dataclass
class _Session:
    controls: list[_Control]
    commits: int = 0

    async def scalars(self, _statement: object) -> _Session:
        return self

    def all(self) -> list[_Control]:
        return self.controls

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


@dataclass
class _Sessions:
    controls: list[_Control] = field(default_factory=list[_Control])
    session: _Session | None = None

    def __call__(self) -> _Session:
        self.session = _Session(self.controls)
        return self.session


def _actions(*controls: _Control) -> tuple[MySqlProtectionSafetyActions, _Sessions]:
    sessions = _Sessions(controls=list(controls))
    return (
        MySqlProtectionSafetyActions(
            sessions=sessions,  # pyright: ignore[reportArgumentType]
            binding_id=BINDING,
        ),
        sessions,
    )


@pytest.mark.asyncio
async def test_stopping_new_exposure_is_not_halting() -> None:
    """A halt would also stop the protective order from being placed or
    filled, which is the opposite of what an unprotected position needs."""
    control = _Control()
    actions, _ = _actions(control)

    await actions.cancel_entry_and_adds(BINDING)

    assert control.kill_switch_level == "BLOCK_NEW_EXPOSURE"
    assert control.armed is True


@pytest.mark.asyncio
async def test_halting_disarms_as_well() -> None:
    """A halt that left the account armed would be a halt in name only."""
    control = _Control()
    actions, _ = _actions(control)

    await actions.halt_account(BINDING, "BINANCE_PROTECTION_DEADLINE")

    assert control.kill_switch_level == "EMERGENCY"
    assert control.armed is False


@pytest.mark.asyncio
async def test_a_stronger_level_already_set_is_never_weakened() -> None:
    control = _Control(kill_switch_level="EMERGENCY", armed=False)
    actions, _ = _actions(control)

    await actions.cancel_entry_and_adds(BINDING)

    assert control.kill_switch_level == "EMERGENCY"


@pytest.mark.asyncio
async def test_a_level_this_code_does_not_know_is_treated_as_the_strongest() -> None:
    """So a row written by something newer is never quietly weakened."""
    control = _Control(kill_switch_level="SOMETHING_ELSE")
    actions, _ = _actions(control)

    await actions.cancel_entry_and_adds(BINDING)
    await actions.halt_account(BINDING, "reason")

    assert control.kill_switch_level == "SOMETHING_ELSE"
    # The halt still disarms: the level it could not raise is not the only
    # thing a halt is for.
    assert control.armed is False


@pytest.mark.asyncio
async def test_every_control_row_moves_together() -> None:
    """One row per scope, and an account in trouble is in trouble at all of
    them - the money is the same money."""
    rows = (_Control(), _Control(kill_switch_level="BLOCK_NEW_EXPOSURE"))
    actions, _ = _actions(*rows)

    await actions.halt_account(BINDING, "reason")

    assert all(row.kill_switch_level == "EMERGENCY" for row in rows)
    assert not any(row.armed for row in rows)


@pytest.mark.asyncio
async def test_another_bindings_trouble_is_refused() -> None:
    control = _Control()
    actions, _ = _actions(control)

    for call in (
        actions.cancel_entry_and_adds(uuid7()),
        actions.halt_account(uuid7(), "reason"),
    ):
        with pytest.raises(ValueError):
            await call
    assert control.kill_switch_level == "NONE"


@pytest.mark.asyncio
async def test_a_halt_with_no_reason_is_refused() -> None:
    """A halt nobody can act on later is a halt that will be undone blind."""
    control = _Control()
    actions, _ = _actions(control)

    for reason in ("", "   "):
        with pytest.raises(ValueError):
            await actions.halt_account(BINDING, reason)
    assert control.kill_switch_level == "NONE"
    assert control.armed is True
