"""The gate that now runs where a command is claimed.

`decide_dispatch` had no callers, so the fencing token, the arming lease and
the trading control were checked by nothing that ran, and a process that had
lost its lease could still have written. §31.12.

Wiring a gate onto a critical path has its own failure mode: one that refuses
everything looks exactly like one that works, because nothing it guards was
running anyway. So the first test here is that a good dispatch is allowed, and
the rest are the ways it is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid7

import pytest

from autotrader.config.settings import RuntimeMode
from autotrader.execution.orders.models import CommandType
from autotrader.persistence.mysql.dispatch_store import (
    MySqlDispatchStore,
    RuntimeFacts,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
OWNER = uuid7()
COMMAND = uuid7()


@dataclass
class _Command:
    id: UUID = field(default_factory=lambda: COMMAND)
    not_after: datetime = NOW + timedelta(minutes=2)
    dispatch_attempted_at: datetime | None = None
    owner_runtime_instance_id: UUID | None = OWNER
    fencing_token: int = 7
    authority_class: str = "SUBMIT_NEW_EXPOSURE"
    command_type: str = CommandType.SUBMIT.value
    result_state: str | None = None
    quantity: Decimal = Decimal("0.002")


@dataclass
class _Lease:
    owner_runtime_instance_id: UUID | None = OWNER
    fencing_token: int = 7
    expires_at: datetime | None = NOW + timedelta(minutes=5)


@dataclass
class _Control:
    owner_runtime_instance_id: UUID | None = OWNER
    fencing_token: int = 7
    acquired_at: datetime | None = NOW - timedelta(minutes=1)
    expires_at: datetime | None = NOW + timedelta(minutes=5)
    armed: bool = True
    kill_switch_level: str = "NONE"
    row_version: int = 1


@dataclass
class _Session:
    command: _Command | None
    leases: list[_Lease]
    controls: list[_Control]
    counts: list[int] = field(default_factory=lambda: [0, 0])
    controls_only: bool = False

    async def get(self, _model: object, _identity: object) -> _Command | None:
        return self.command

    async def scalars(self, _statement: object) -> _Session:
        # `_may_dispatch` asks for the leases then the controls;
        # `_may_submit` asks only for the controls.
        if self.controls_only or not self._wants_leases:
            self._pending = list(self.controls)
        else:
            self._pending = list(self.leases)
        self._wants_leases = not self._wants_leases
        return self

    def all(self) -> list[object]:
        return list(self._pending)

    async def scalar(self, _statement: object) -> int:
        return self.counts.pop(0)

    _wants_leases: bool = True
    _pending: list[object] = field(default_factory=list[object])


def _store(session: _Session) -> MySqlDispatchStore:
    return MySqlDispatchStore(session)  # pyright: ignore[reportArgumentType]


async def _allowed(**changes: object) -> bool:
    session = _Session(
        command=_Command(**{k: v for k, v in changes.items() if k in _COMMAND_FIELDS}),  # pyright: ignore[reportArgumentType]
        leases=[_Lease(**{k: v for k, v in changes.items() if k in _LEASE_FIELDS})],  # pyright: ignore[reportArgumentType]
        controls=[
            _Control(**{k: v for k, v in changes.items() if k in _CONTROL_FIELDS})  # pyright: ignore[reportArgumentType]
        ],
        counts=[
            int(changes.get("blocking", 0)),  # pyright: ignore[reportArgumentType]
            int(changes.get("unknown", 0)),  # pyright: ignore[reportArgumentType]
        ],
    )
    if changes.get("no_controls"):
        session.controls = []
    if changes.get("no_lease"):
        session.leases = []
    return await _store(session)._may_dispatch(COMMAND, NOW)  # pyright: ignore[reportPrivateUsage]


_COMMAND_FIELDS = frozenset(_Command.__dataclass_fields__)
_LEASE_FIELDS = frozenset(_Lease.__dataclass_fields__) - _COMMAND_FIELDS
_CONTROL_FIELDS = frozenset(_Control.__dataclass_fields__) - _COMMAND_FIELDS


@pytest.mark.asyncio
async def test_a_good_dispatch_is_allowed() -> None:
    """The one that matters: a gate refusing everything would look identical
    to a working one, because nothing it guards was running before."""
    assert await _allowed() is True


@pytest.mark.asyncio
async def test_a_command_already_attempted_is_refused() -> None:
    assert await _allowed(dispatch_attempted_at=NOW) is False


@pytest.mark.asyncio
async def test_an_expired_command_is_refused() -> None:
    assert await _allowed(not_after=NOW - timedelta(seconds=1)) is False


@pytest.mark.asyncio
async def test_a_stale_fencing_token_is_refused() -> None:
    """The thing the gate exists for: an instance that lost its lease, and a
    newer one took the token, may not still write."""
    assert await _allowed(fencing_token=6) is False


@pytest.mark.asyncio
async def test_a_lease_owned_by_someone_else_is_refused() -> None:
    assert await _allowed(owner_runtime_instance_id=uuid7()) is False


@pytest.mark.asyncio
async def test_a_missing_lease_is_refused() -> None:
    assert await _allowed(no_lease=True) is False


@pytest.mark.asyncio
async def test_a_disarmed_control_is_refused() -> None:
    assert await _allowed(armed=False) is False


@pytest.mark.asyncio
async def test_a_kill_switch_at_any_level_is_refused() -> None:
    for level in ("BLOCK_NEW_EXPOSURE", "EMERGENCY"):
        assert await _allowed(kill_switch_level=level) is False, level


@pytest.mark.asyncio
async def test_no_control_row_is_not_armed() -> None:
    """Nobody armed anything, and with no rows there is nothing to run the
    gate against - which is a refusal, not a pass."""
    assert await _allowed(no_controls=True) is False


@pytest.mark.asyncio
async def test_a_blocking_incident_or_unknown_order_refuses() -> None:
    assert await _allowed(blocking=1) is False
    assert await _allowed(unknown=1) is False


@pytest.mark.asyncio
async def test_a_closing_order_is_allowed_past_the_arming_checks() -> None:
    """Refusing to reduce exposure is not a safe default: a disarmed account
    still has to be able to close what it holds."""
    assert (
        await _allowed(authority_class="SUBMIT_STRICT_REDUCTION", armed=False) is True
    )


@pytest.mark.asyncio
async def test_a_cancel_is_allowed_past_them_too() -> None:
    assert await _allowed(command_type=CommandType.CANCEL.value, armed=False) is True


@pytest.mark.asyncio
async def test_a_closing_order_still_needs_its_lease() -> None:
    """Being allowed past arming is not being allowed past fencing: two
    instances closing one position is still two instances."""
    assert (
        await _allowed(authority_class="SUBMIT_STRICT_REDUCTION", fencing_token=6)
        is False
    )


# --- the runtime half: what `allow_live` decides -------------------------


def _facts(**changes: object) -> RuntimeFacts:
    values: dict[str, object] = {
        "runtime_mode": RuntimeMode.PAPER,
        "allow_live": False,
        "account_environment": RuntimeMode.PAPER,
        "local_runtime_instance_id": OWNER,
        "market_data_fresh": lambda: True,
    }
    values.update(changes)
    return RuntimeFacts(**values)  # pyright: ignore[reportArgumentType]


async def _submittable(facts: RuntimeFacts, **changes: object) -> bool:
    session = _Session(
        command=_Command(
            **{k: v for k, v in changes.items() if k in _COMMAND_FIELDS}  # pyright: ignore[reportArgumentType]
        ),
        leases=[_Lease()],
        controls=[
            _Control(**{k: v for k, v in changes.items() if k in _CONTROL_FIELDS})  # pyright: ignore[reportArgumentType]
        ],
        counts=[0, 0, 0],
        controls_only=True,
    )
    store = MySqlDispatchStore(session, facts)  # pyright: ignore[reportArgumentType]
    return await store._may_submit(COMMAND, NOW)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_a_paper_submission_is_allowed() -> None:
    assert await _submittable(_facts()) is True


@pytest.mark.asyncio
async def test_live_without_permission_is_refused() -> None:
    """The switch between a LIVE build existing and LIVE being able to trade.
    Nothing that ran was looking at it. §31.12."""
    assert (
        await _submittable(
            _facts(
                runtime_mode=RuntimeMode.LIVE,
                account_environment=RuntimeMode.LIVE,
                allow_live=False,
            )
        )
        is False
    )


@pytest.mark.asyncio
async def test_live_with_permission_and_a_matching_account_is_allowed() -> None:
    assert (
        await _submittable(
            _facts(
                runtime_mode=RuntimeMode.LIVE,
                account_environment=RuntimeMode.LIVE,
                allow_live=True,
            )
        )
        is True
    )


@pytest.mark.asyncio
async def test_a_live_run_against_a_paper_account_is_refused() -> None:
    """And a paper run against a live account, which is the direction that
    would put real money behind a rehearsal."""
    assert (
        await _submittable(
            _facts(
                runtime_mode=RuntimeMode.LIVE,
                account_environment=RuntimeMode.PAPER,
                allow_live=True,
            )
        )
        is False
    )
    assert (
        await _submittable(
            _facts(
                runtime_mode=RuntimeMode.PAPER,
                account_environment=RuntimeMode.LIVE,
            )
        )
        is False
    )


@pytest.mark.asyncio
async def test_shadow_never_submits() -> None:
    """It has no execution port at all, and if one ever reached here the mode
    alone refuses it."""
    assert await _submittable(_facts(runtime_mode=RuntimeMode.SHADOW)) is False


@pytest.mark.asyncio
async def test_stale_market_data_refuses_a_market_order() -> None:
    assert await _submittable(_facts(market_data_fresh=lambda: False)) is False


@pytest.mark.asyncio
async def test_a_store_built_without_facts_authorises_nothing() -> None:
    """Reading and recovery need no runtime facts; sending does. A store that
    was not told refuses rather than assuming."""
    session = _Session(
        command=_Command(),
        leases=[_Lease()],
        controls=[_Control()],
        controls_only=True,
    )
    store = MySqlDispatchStore(session)  # pyright: ignore[reportArgumentType]
    assert await store._may_submit(COMMAND, NOW) is False  # pyright: ignore[reportPrivateUsage]
