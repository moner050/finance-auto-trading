"""Turning a termination signal into the stop the loop understands.

The reason this exists is not the summary line. It is where an uncaught
termination lands: between fetching a page of trades and storing it, with the
checkpoint saying one thing and the tape another. The tests below are about
the handler being installed, reaching the stop, and putting back what it
found - a run that leaves the process's signal handling rearranged is a run
that changed something it was never asked to touch.
"""

from __future__ import annotations

import asyncio
import signal

import pytest

from autotrader.apps.trader.run_shadow import SHUTDOWN_SIGNALS, stop_on_signals


@pytest.mark.asyncio
async def test_a_signal_reaches_the_stop() -> None:
    stop = asyncio.Event()

    async with stop_on_signals(stop):
        signal.raise_signal(signal.SIGTERM)
        # The handler hands the set to the loop rather than doing it inline,
        # so it lands on the next turn rather than this one.
        for _ in range(40):
            if stop.is_set():
                break
            await asyncio.sleep(0.01)

    assert stop.is_set()


@pytest.mark.asyncio
async def test_what_was_there_before_is_put_back() -> None:
    """A library that leaves the process's signal handling rearranged has
    changed something nobody asked it to."""
    before = {number: signal.getsignal(number) for number in SHUTDOWN_SIGNALS}

    async with stop_on_signals(asyncio.Event()):
        installed = {number: signal.getsignal(number) for number in SHUTDOWN_SIGNALS}

    assert all(installed[number] is not before[number] for number in SHUTDOWN_SIGNALS)
    assert {number: signal.getsignal(number) for number in SHUTDOWN_SIGNALS} == before


@pytest.mark.asyncio
async def test_the_second_signal_is_not_treated_as_a_repeat() -> None:
    """A second signal means the first one did not work. The handler steps
    aside so the next one kills, rather than politely asking again."""
    before = signal.getsignal(signal.SIGTERM)
    stop = asyncio.Event()

    async with stop_on_signals(stop, numbers=(signal.SIGTERM,)):
        signal.raise_signal(signal.SIGTERM)
        after_first = signal.getsignal(signal.SIGTERM)

    assert after_first is before


@pytest.mark.asyncio
async def test_handlers_are_restored_even_when_the_body_raises() -> None:
    before = {number: signal.getsignal(number) for number in SHUTDOWN_SIGNALS}

    with pytest.raises(RuntimeError, match="the venue disagreed"):
        async with stop_on_signals(asyncio.Event()):
            raise RuntimeError("the venue disagreed")

    assert {number: signal.getsignal(number) for number in SHUTDOWN_SIGNALS} == before


@pytest.mark.asyncio
async def test_a_signal_this_platform_refuses_does_not_stop_the_run() -> None:
    """A run that cannot be asked to stop politely is still a run worth
    having, so an unavailable signal is skipped rather than raised."""
    stop = asyncio.Event()
    unavailable = 64  # Outside the range any platform here defines.

    async with stop_on_signals(stop, numbers=(unavailable,)):  # type: ignore[arg-type]
        pass

    assert not stop.is_set()
