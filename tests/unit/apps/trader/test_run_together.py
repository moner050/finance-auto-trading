"""Neither the stream nor the loop is useful alone.

Without the tape advancing, every pass quietly produces nothing - the inputs
cannot rank a delta or an ATR over an empty window - so a loop left running
would look alive and decide nothing. Without the loop, the stream fills a
table nobody reads.

So whichever ends first ends both, and its failure is the run's. The case
that matters is the stream failing on a correction conflict: that has to
reach the operator rather than leave a loop evaluating a tape that stopped
being trustworthy.
"""

from __future__ import annotations

import asyncio

import pytest

from autotrader.apps.trader.run_shadow import run_together


class _Tracked:
    """A coroutine that records whether it was allowed to finish."""

    def __init__(self, *, seconds: float, fail: Exception | None = None) -> None:
        self._seconds = seconds
        self._fail = fail
        self.finished = False
        self.cancelled = False

    async def run(self) -> None:
        try:
            await asyncio.sleep(self._seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self._fail is not None:
            raise self._fail
        self.finished = True


@pytest.mark.asyncio
async def test_a_failing_stream_stops_the_loop_and_is_raised() -> None:
    """A correction conflict must reach the operator, not leave the loop
    evaluating a tape that stopped being trustworthy."""
    stream = _Tracked(seconds=0.01, fail=RuntimeError("correction conflict"))
    loop = _Tracked(seconds=10)
    stop = asyncio.Event()

    with pytest.raises(RuntimeError, match="correction conflict"):
        await run_together(stream=stream.run(), loop=loop.run(), stop=stop)

    assert loop.cancelled is True
    assert stop.is_set()


@pytest.mark.asyncio
async def test_a_failing_loop_stops_the_stream_and_is_raised() -> None:
    stream = _Tracked(seconds=10)
    loop = _Tracked(seconds=0.01, fail=RuntimeError("loop gave up"))
    stop = asyncio.Event()

    with pytest.raises(RuntimeError, match="loop gave up"):
        await run_together(stream=stream.run(), loop=loop.run(), stop=stop)

    assert stream.cancelled is True


@pytest.mark.asyncio
async def test_one_ending_cleanly_still_ends_the_other() -> None:
    """A stream that returns has stopped feeding the tape, which is not a
    state the loop should keep running in."""
    stream = _Tracked(seconds=0.01)
    loop = _Tracked(seconds=10)
    stop = asyncio.Event()

    await run_together(stream=stream.run(), loop=loop.run(), stop=stop)

    assert stream.finished is True
    assert loop.cancelled is True
    assert stop.is_set()


@pytest.mark.asyncio
async def test_the_stop_event_is_set_for_both_to_see() -> None:
    """Both take the same event, so setting it is how each is asked to wind
    down rather than be cut off mid-write."""
    stop = asyncio.Event()

    async def watches_the_flag() -> None:
        while not stop.is_set():
            await asyncio.sleep(0.005)

    await run_together(
        stream=_Tracked(seconds=0.01).run(), loop=watches_the_flag(), stop=stop
    )

    assert stop.is_set()
