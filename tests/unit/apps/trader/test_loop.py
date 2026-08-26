from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from unit.apps.trader.test_tick import (
    _bare_context,
    _Control,
    _Execution,
    _Recorder,
    _setup_context,
)

from autotrader.apps.trader.loop import (
    NO_NEW_BAR,
    NOT_LEADER,
    UNPROTECTED,
    LoopPorts,
    SystemClock,
    run_forever,
    run_pass,
)
from autotrader.apps.trader.tick import DISARMED, TickContext

NOW = datetime(2026, 8, 25, 13, 30, tzinfo=UTC)


class _Lease:
    def __init__(self, held: bool) -> None:
        self.held = held
        self.acquired = 0

    async def acquire(self, now: datetime) -> bool:
        del now
        self.acquired += 1
        return self.held


class _Settlement:
    def __init__(self, settled: int = 0) -> None:
        self._settled = settled
        self.calls = 0

    async def settle(self, now: datetime) -> int:
        del now
        self.calls += 1
        return self._settled


class _Source:
    def __init__(self, context: TickContext | None) -> None:
        self._context = context
        self.calls = 0

    async def context_for(self, now: datetime) -> TickContext | None:
        del now
        self.calls += 1
        return self._context


class _Protection:
    """How many open positions have no stop behind them."""

    def __init__(self, unprotected: int = 0) -> None:
        self._unprotected = unprotected
        self.calls = 0

    async def unprotected(self, now: datetime) -> int:
        del now
        self.calls += 1
        return self._unprotected


def _ports(
    *,
    lease: _Lease,
    settlement: _Settlement,
    source: _Source,
    control: _Control | None = None,
    recorder: _Recorder | None = None,
    execution: _Execution | None = None,
    protection: _Protection | None = None,
) -> LoopPorts:
    return LoopPorts(
        lease=lease,
        settlement=settlement,
        protection=protection or _Protection(),
        source=source,
        control=control or _Control(True),
        recorder=recorder or _Recorder(),
        execution=execution or _Execution(),
    )


@pytest.mark.asyncio
async def test_without_the_lease_the_pass_does_nothing_at_all() -> None:
    lease, settlement, source = _Lease(False), _Settlement(), _Source(_setup_context())
    recorder = _Recorder()

    result = await run_pass(
        now=NOW,
        ports=_ports(
            lease=lease, settlement=settlement, source=source, recorder=recorder
        ),
    )

    assert result.reason == NOT_LEADER
    assert result.outcome is None
    # Two loops trading one account is the worst outcome, so a follower
    # settles nothing and evaluates nothing.
    assert settlement.calls == 0
    assert source.calls == 0
    assert recorder.recorded == []


@pytest.mark.asyncio
async def test_fills_settle_before_the_evaluation_runs() -> None:
    order: list[str] = []

    class _OrderedSettlement(_Settlement):
        async def settle(self, now: datetime) -> int:
            order.append("settle")
            return await super().settle(now)

    class _OrderedSource(_Source):
        async def context_for(self, now: datetime) -> TickContext | None:
            order.append("evaluate")
            return await super().context_for(now)

    settlement = _OrderedSettlement(2)
    result = await run_pass(
        now=NOW,
        ports=_ports(
            lease=_Lease(True),
            settlement=settlement,
            source=_OrderedSource(_bare_context()),
        ),
    )

    # The evaluation should see positions as they actually stand.
    assert order == ["settle", "evaluate"]
    assert result.settled == 2


@pytest.mark.asyncio
async def test_no_new_bar_still_settles_and_evaluates_nothing() -> None:
    recorder = _Recorder()
    settlement = _Settlement(1)

    result = await run_pass(
        now=NOW,
        ports=_ports(
            lease=_Lease(True),
            settlement=settlement,
            source=_Source(None),
            recorder=recorder,
        ),
    )

    assert result.reason == NO_NEW_BAR
    assert result.settled == 1
    assert result.outcome is None
    assert recorder.recorded == []


@pytest.mark.asyncio
async def test_a_pass_carries_the_tick_reason_through() -> None:
    result = await run_pass(
        now=NOW,
        ports=_ports(
            lease=_Lease(True),
            settlement=_Settlement(),
            source=_Source(_setup_context()),
            control=_Control(False),
        ),
    )

    assert result.reason == DISARMED
    assert result.outcome is not None
    assert result.outcome.decision is None


@pytest.mark.asyncio
async def test_the_loop_runs_passes_until_it_is_asked_to_stop() -> None:
    lease = _Lease(True)
    settlement = _Settlement()
    stop = asyncio.Event()

    class _CountingClock:
        def __init__(self) -> None:
            self.slept = 0

        def now(self) -> datetime:
            return NOW

        async def sleep(self, seconds: float) -> None:
            del seconds
            self.slept += 1
            if self.slept >= 3:
                stop.set()

    clock = _CountingClock()
    await run_forever(
        ports=_ports(lease=lease, settlement=settlement, source=_Source(None)),
        clock=clock,
        interval=timedelta(seconds=1),
        stop=stop,
    )

    assert lease.acquired == 3
    assert settlement.calls == 3


@pytest.mark.asyncio
async def test_a_loop_asked_to_stop_first_never_runs_a_pass() -> None:
    lease = _Lease(True)
    stop = asyncio.Event()
    stop.set()

    class _Clock:
        def now(self) -> datetime:
            return NOW

        async def sleep(self, seconds: float) -> None:
            del seconds

    await run_forever(
        ports=_ports(lease=lease, settlement=_Settlement(), source=_Source(None)),
        clock=_Clock(),
        interval=timedelta(seconds=1),
        stop=stop,
    )

    assert lease.acquired == 0


@pytest.mark.asyncio
async def test_the_interval_must_be_positive() -> None:
    class _Clock:
        def now(self) -> datetime:
            return NOW

        async def sleep(self, seconds: float) -> None:
            del seconds

    with pytest.raises(ValueError, match="interval must be positive"):
        await run_forever(
            ports=_ports(
                lease=_Lease(True), settlement=_Settlement(), source=_Source(None)
            ),
            clock=_Clock(),
            interval=timedelta(0),
            stop=asyncio.Event(),
        )


@pytest.mark.asyncio
async def test_ports_must_be_exact() -> None:
    with pytest.raises(TypeError, match="exact LoopPorts"):
        await run_pass(now=NOW, ports=object())  # type: ignore[arg-type]


def test_the_system_clock_reports_whole_seconds() -> None:
    """The stored columns have no fractional seconds, so MySQL would round."""
    moment = SystemClock().now()

    assert moment.tzinfo is UTC
    assert moment.microsecond == 0


@pytest.mark.asyncio
async def test_an_unprotected_position_stops_the_pass_before_it_evaluates() -> None:
    """Opening more while something already held has no stop is the one thing
    the loop must not do, so it does not even look at the bar."""
    source = _Source(_setup_context())
    settlement = _Settlement(2)
    result = await run_pass(
        now=NOW,
        ports=_ports(
            lease=_Lease(True),
            settlement=settlement,
            source=source,
            protection=_Protection(1),
        ),
    )

    assert result.reason == UNPROTECTED
    assert result.outcome is None
    # Settling still happened: that is how a stop gets resolved at all.
    assert result.settled == 2
    assert source.calls == 0


@pytest.mark.asyncio
async def test_protection_is_checked_only_after_settling() -> None:
    """A stop filled on this pass takes the position flat, so checking before
    settling would report a position that no longer exists."""
    protection = _Protection()
    settlement = _Settlement(1)
    await run_pass(
        now=NOW,
        ports=_ports(
            lease=_Lease(True),
            settlement=settlement,
            source=_Source(_setup_context()),
            protection=protection,
        ),
    )

    assert settlement.calls == 1
    assert protection.calls == 1


@pytest.mark.asyncio
async def test_without_the_lease_nothing_is_checked_either() -> None:
    protection = _Protection(1)
    result = await run_pass(
        now=NOW,
        ports=_ports(
            lease=_Lease(False),
            settlement=_Settlement(),
            source=_Source(_setup_context()),
            protection=protection,
        ),
    )

    assert result.reason == NOT_LEADER
    # Another instance owns this account, and owns the question too.
    assert protection.calls == 0
