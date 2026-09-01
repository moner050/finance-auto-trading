"""Fetching the tape instead of being pushed it.

The cases that matter are where the poller decides what to ask for. Resuming
from the wrong id either replays the venue's whole history or skips trades the
strategy then never sees, and neither announces itself: the tape simply has
the wrong contents while everything reports healthy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from autotrader.integrations.market_data.binance_trade_poller import (
    PAGE_LIMIT,
    BinanceUsdmTradePoller,
)


def _row(trade_id: int) -> dict[str, object]:
    return {
        "a": trade_id,
        "f": trade_id,
        "l": trade_id,
        "T": 1_800_000_000_000 + trade_id,
        "p": "78000.0",
        "q": "0.01",
        "m": False,
    }


class _Rest:
    def __init__(self, *pages: tuple[dict[str, object], ...]) -> None:
        self._pages = list(pages)
        self.asked: list[int | None] = []

    async def aggregate_trades(
        self, *, symbol: str, from_id: int | None, limit: int
    ) -> tuple[object, ...]:
        del symbol, limit
        self.asked.append(from_id)
        return self._pages.pop(0) if self._pages else ()


class _Failing(_Rest):
    def __init__(self, failures: int, *pages: tuple[dict[str, object], ...]) -> None:
        super().__init__(*pages)
        self._failures = failures

    async def aggregate_trades(
        self, *, symbol: str, from_id: int | None, limit: int
    ) -> tuple[object, ...]:
        if self._failures > 0:
            self._failures -= 1
            raise ConnectionError("the venue is unreachable")
        return await super().aggregate_trades(
            symbol=symbol, from_id=from_id, limit=limit
        )


class _Store:
    def __init__(self, checkpoint: int | None = None) -> None:
        self.checkpoint = checkpoint
        self.seen: list[int] = []

    async def checkpoint_trade_id(self) -> int | None:
        return self.checkpoint

    async def ingest_rest_agg_trade(self, row: Mapping[str, object]) -> None:
        trade_id = int(str(row["a"]))
        self.seen.append(trade_id)
        self.checkpoint = trade_id


class _Sleeps:
    def __init__(self, stop: asyncio.Event, *, stop_after: int = 1) -> None:
        self.waited: list[float] = []
        self._stop = stop
        self._stop_after = stop_after

    async def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)
        if len(self.waited) >= self._stop_after:
            self._stop.set()


def _poller(store: _Store, rest: _Rest, sleeps: object) -> BinanceUsdmTradePoller:
    return BinanceUsdmTradePoller(
        market_data=store,  # type: ignore[arg-type]
        rest=rest,  # type: ignore[arg-type]
        sleep=sleeps,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_an_unread_tape_starts_from_the_most_recent_page() -> None:
    """Not from zero. Zero is a real id at the start of the venue's history,
    and the first poll would begin in 2019 and page forward for a long time
    before reaching today."""
    stop = asyncio.Event()
    store = _Store(checkpoint=None)
    rest = _Rest((_row(500), _row(501)))

    await _poller(store, rest, _Sleeps(stop)).run(stop=stop)

    assert rest.asked[0] is None
    assert store.seen == [500, 501]


@pytest.mark.asyncio
async def test_a_read_tape_resumes_from_the_next_unseen_id() -> None:
    """Asking from the checkpoint itself would refetch a trade already stored
    on every single poll."""
    stop = asyncio.Event()
    store = _Store(checkpoint=42)
    rest = _Rest((_row(43),))

    await _poller(store, rest, _Sleeps(stop)).run(stop=stop)

    assert rest.asked[0] == 43


@pytest.mark.asyncio
async def test_a_full_page_is_followed_immediately() -> None:
    """A full page means the tape has more waiting. Sleeping the interval
    between pages would make catching up take as long as the backlog."""
    stop = asyncio.Event()
    store = _Store(checkpoint=0)
    full = tuple(_row(index) for index in range(1, PAGE_LIMIT + 1))
    rest = _Rest(full, (_row(PAGE_LIMIT + 1),))
    sleeps = _Sleeps(stop, stop_after=1)

    await _poller(store, rest, sleeps).run(stop=stop)

    # Two fetches, one sleep: the full page did not wait.
    assert len(rest.asked) == 2
    assert len(sleeps.waited) == 1


@pytest.mark.asyncio
async def test_an_unreachable_venue_backs_off_and_retries() -> None:
    """Reaching the venue is allowed to fail. What it sends once it arrives
    is not."""
    stop = asyncio.Event()
    store = _Store(checkpoint=1)
    rest = _Failing(2, (_row(2),))
    sleeps = _Sleeps(stop, stop_after=3)

    poller = _poller(store, rest, sleeps)
    await poller.run(stop=stop)

    assert poller.failures == 2
    assert sleeps.waited[0] == 1.0
    assert sleeps.waited[1] == 2.0
    assert store.seen == [2]


@pytest.mark.asyncio
async def test_an_out_of_sequence_trade_is_not_swallowed() -> None:
    """The store refuses it, and the poller must let that reach the operator
    rather than treat it as another fetch that did not work."""
    stop = asyncio.Event()

    class _Refusing(_Store):
        async def ingest_rest_agg_trade(self, row: Mapping[str, object]) -> None:
            raise ValueError("Binance USD-M aggregate trade sequence is broken")

    store = _Refusing(checkpoint=1)
    rest = _Rest((_row(9),))

    with pytest.raises(ValueError, match="sequence is broken"):
        await _poller(store, rest, _Sleeps(stop)).run(stop=stop)


@pytest.mark.asyncio
async def test_stopping_ends_the_run() -> None:
    stop = asyncio.Event()
    stop.set()
    store = _Store(checkpoint=1)
    rest = _Rest((_row(2),))

    await _poller(store, rest, _Sleeps(stop)).run(stop=stop)

    assert store.seen == []
    assert rest.asked == []


def test_a_page_larger_than_the_venue_allows_is_refused() -> None:
    with pytest.raises(ValueError, match="page limit"):
        BinanceUsdmTradePoller(
            market_data=_Store(),  # type: ignore[arg-type]
            rest=_Rest(),  # type: ignore[arg-type]
            limit=PAGE_LIMIT + 1,
        )
