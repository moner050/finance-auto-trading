"""A run that knows when it is due to finish.

On Windows nothing outside the process can ask it to stop: `timeout` and
`taskkill` terminate through the Win32 API, where no signal is delivered and
no handler runs. So the length has to come from inside, and the number that
sets it has to be read exactly - `--for 30` meaning half a minute to one
operator and half an hour to another is a factor of sixty on a live account.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from autotrader.apps.trader.__main__ import parse_duration
from autotrader.apps.trader.run_shadow import stop_after


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("900s", timedelta(seconds=900)),
        ("90m", timedelta(minutes=90)),
        ("6h", timedelta(hours=6)),
        ("1s", timedelta(seconds=1)),
    ],
)
def test_a_stated_length_is_read_as_stated(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


def test_a_bare_number_is_refused_rather_than_guessed_at() -> None:
    with pytest.raises(ValueError, match="number and a unit"):
        parse_duration("30")


@pytest.mark.parametrize("text", ["6d", "6w", "6x", "sixh"])
def test_an_unknown_unit_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(text)


@pytest.mark.parametrize("text", ["0h", "0s"])
def test_a_zero_length_run_is_refused(text: str) -> None:
    """It would start, stop before evaluating anything, and report a clean
    session that observed nothing."""
    with pytest.raises(ValueError, match="positive"):
        parse_duration(text)


def test_a_negative_length_is_refused() -> None:
    with pytest.raises(ValueError, match="not a whole number"):
        parse_duration("-5m")


def test_a_fractional_length_is_refused() -> None:
    with pytest.raises(ValueError, match="not a whole number"):
        parse_duration("1.5h")


@pytest.mark.asyncio
async def test_the_run_ends_itself_when_the_length_elapses() -> None:
    stop = asyncio.Event()

    async with stop_after(stop, timedelta(milliseconds=30)):
        await asyncio.sleep(0.2)

    assert stop.is_set()


@pytest.mark.asyncio
async def test_no_length_means_the_run_continues() -> None:
    stop = asyncio.Event()

    async with stop_after(stop, None):
        await asyncio.sleep(0.05)

    assert not stop.is_set()


@pytest.mark.asyncio
async def test_the_timer_does_not_outlive_the_run() -> None:
    """A run that ended early leaves a sleeping task behind if the timer is
    not cancelled, and the event loop will not close under it."""
    stop = asyncio.Event()

    async with stop_after(stop, timedelta(hours=1)):
        pass

    assert not stop.is_set()
    assert len([task for task in asyncio.all_tasks() if not task.done()]) == 1


@pytest.mark.asyncio
async def test_a_zero_duration_is_refused_here_too() -> None:
    """The parser is not the only way in - a caller can pass a timedelta."""
    with pytest.raises(ValueError, match="positive"):
        async with stop_after(asyncio.Event(), timedelta(0)):
            pass
