"""Keeping the lease alive between passes.

The defect this exists for was invisible: five-minute passes renewing a
two-minute lease left the account unclaimed for three minutes out of every
five, and nothing reported it because the tape poller happened to renew every
couple of seconds. A correctness property propped up by an unrelated component
is the same as not having one - it holds until somebody changes the other
thing for an unrelated reason.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from autotrader.apps.trader.run_shadow import (
    LEASE_RENEWAL_FRACTION,
    LEASE_TTL,
    LeaseHeartbeat,
)


class _Lease:
    def __init__(self, *, held: bool = True) -> None:
        self.held = held
        self.moments: list[datetime] = []

    async def acquire(self, now: datetime) -> bool:
        self.moments.append(now)
        return self.held


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


class _Sleeps:
    def __init__(self, stop: asyncio.Event, *, stop_after: int) -> None:
        self.waited: list[float] = []
        self._stop = stop
        self._stop_after = stop_after

    async def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)
        if len(self.waited) >= self._stop_after:
            self._stop.set()


def _heartbeat(
    lease: _Lease, sleeps: object, ttl: timedelta = LEASE_TTL
) -> LeaseHeartbeat:
    return LeaseHeartbeat(
        lease=lease,  # type: ignore[arg-type]
        clock=_Clock(),
        ttl=ttl,
        sleep=sleeps,  # type: ignore[arg-type]
    )


def test_the_cadence_is_shorter_than_the_term() -> None:
    """The whole point. Renewing on any schedule longer than the term means
    the lease expires under a process that is still running."""
    assert LEASE_RENEWAL_FRACTION > 1


@pytest.mark.asyncio
async def test_renewal_is_derived_from_the_term_not_configured_beside_it() -> None:
    """Two numbers can drift apart; one cannot. A renewal interval set
    separately is a renewal interval somebody will later change without
    looking at the term."""
    stop = asyncio.Event()
    sleeps = _Sleeps(stop, stop_after=2)

    await _heartbeat(_Lease(), sleeps, timedelta(minutes=3)).run(stop=stop)

    assert sleeps.waited == [60.0, 60.0]


@pytest.mark.asyncio
async def test_the_lease_is_renewed_on_that_cadence() -> None:
    """Three sleeps, two renewals: the third sleep is the one the stop lands
    in, and a run that is ending does not extend its own claim - leaving it to
    expire hands the account to a successor a term sooner."""
    stop = asyncio.Event()
    lease = _Lease()

    beat = _heartbeat(lease, _Sleeps(stop, stop_after=3))
    await beat.run(stop=stop)

    assert beat.renewals == 2
    assert beat.losses == 0
    assert len(lease.moments) == 2


@pytest.mark.asyncio
async def test_losing_the_lease_does_not_end_the_run() -> None:
    """Another instance owns the account. The pass will report NOT_LEADER and
    place nothing, and leadership can come back - which it cannot if this gave
    up asking."""
    stop = asyncio.Event()
    lease = _Lease(held=False)

    beat = _heartbeat(lease, _Sleeps(stop, stop_after=3))
    await beat.run(stop=stop)

    assert beat.losses == 2
    assert beat.renewals == 0


@pytest.mark.asyncio
async def test_stopping_ends_it_without_another_renewal() -> None:
    stop = asyncio.Event()
    stop.set()
    lease = _Lease()

    await _heartbeat(lease, _Sleeps(stop, stop_after=1)).run(stop=stop)

    assert lease.moments == []


def test_a_term_that_is_not_a_term_is_refused() -> None:
    with pytest.raises(ValueError, match="positive"):
        _heartbeat(_Lease(), None, timedelta(0))


class _Journal:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def lost(self, now: datetime) -> None:
        self.events.append("lost")

    async def regained(self, now: datetime) -> None:
        self.events.append("regained")


class _Flipping:
    """A lease that answers from a script, so a run can lose and recover."""

    def __init__(self, answers: list[bool]) -> None:
        self._answers = answers
        self.asked = 0

    async def acquire(self, now: datetime) -> bool:
        answer = self._answers[min(self.asked, len(self._answers) - 1)]
        self.asked += 1
        return answer


def _with_journal(lease: object, sleeps: object, journal: _Journal) -> LeaseHeartbeat:
    return LeaseHeartbeat(
        lease=lease,  # type: ignore[arg-type]
        clock=_Clock(),
        sleep=sleeps,  # type: ignore[arg-type]
        journal=journal,
    )


@pytest.mark.asyncio
async def test_holding_the_lease_is_not_news() -> None:
    """Forty seconds apart for six hours is five hundred renewals. Writing
    one down each time would bury the one that matters."""
    stop = asyncio.Event()
    journal = _Journal()
    await _with_journal(_Lease(held=True), _Sleeps(stop, stop_after=5), journal).run(
        stop=stop
    )

    assert journal.events == []


@pytest.mark.asyncio
async def test_losing_it_is_written_down_once_not_every_attempt() -> None:
    stop = asyncio.Event()
    journal = _Journal()
    await _with_journal(_Lease(held=False), _Sleeps(stop, stop_after=5), journal).run(
        stop=stop
    )

    assert journal.events == ["lost"]


@pytest.mark.asyncio
async def test_getting_it_back_is_written_down_too() -> None:
    """Half the fact. A loop that never recovers keeps evaluating and writing
    nothing, which on the screen looks the same as an hour the loop was down."""
    stop = asyncio.Event()
    journal = _Journal()
    lease = _Flipping([True, True, False, False, True, True])
    await _with_journal(lease, _Sleeps(stop, stop_after=6), journal).run(stop=stop)

    assert journal.events == ["lost", "regained"]


@pytest.mark.asyncio
async def test_a_process_that_never_held_it_still_reports_the_loss() -> None:
    """Starting without the lease is the case worth hearing about, and it is
    the one a "report only changes" rule would drop."""
    stop = asyncio.Event()
    journal = _Journal()
    # One more sleep than acquires: `run` returns on a set stop before it
    # asks again.
    lease = _Flipping([False, True])
    await _with_journal(lease, _Sleeps(stop, stop_after=3), journal).run(stop=stop)

    assert journal.events == ["lost", "regained"]


@pytest.mark.asyncio
async def test_the_counters_still_work_without_a_journal() -> None:
    """The journal is optional so nothing that constructs this today breaks."""
    stop = asyncio.Event()
    heartbeat = _heartbeat(_Lease(held=False), _Sleeps(stop, stop_after=4))
    await heartbeat.run(stop=stop)

    assert heartbeat.losses == 3
    assert heartbeat.renewals == 0
