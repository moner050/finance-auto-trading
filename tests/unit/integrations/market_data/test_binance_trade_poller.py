"""Fetching the tape instead of being pushed it.

The cases that matter are where the poller decides what to ask for. Resuming
from the wrong id either replays the venue's whole history or skips trades the
strategy then never sees, and neither announces itself: the tape simply has
the wrong contents while everything reports healthy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest

from autotrader.integrations.market_data.binance_trade_poller import (
    PAGE_LIMIT,
    POLL_INTERVAL_SECONDS,
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
        self.pages = 0

    async def checkpoint_trade_id(self) -> int | None:
        return self.checkpoint

    async def ingest_rest_agg_trades(
        self, rows: Sequence[Mapping[str, object]]
    ) -> None:
        self.pages += 1
        for row in rows:
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


class _Lease:
    """Leadership, which this instance either has or does not."""

    def __init__(self, *, held: bool = True) -> None:
        self.held = held
        self.asked = 0

    async def acquire(self, now: datetime) -> bool:
        del now
        self.asked += 1
        return self.held


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _poller(
    store: _Store,
    rest: _Rest,
    sleeps: object,
    lease: _Lease | None = None,
) -> BinanceUsdmTradePoller:
    return BinanceUsdmTradePoller(
        market_data=store,  # type: ignore[arg-type]
        rest=rest,  # type: ignore[arg-type]
        lease=lease if lease is not None else _Lease(),
        clock=_Clock(),
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
        async def ingest_rest_agg_trades(
            self, rows: Sequence[Mapping[str, object]]
        ) -> None:
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
            lease=_Lease(),  # type: ignore[arg-type]
            clock=_Clock(),
            limit=PAGE_LIMIT + 1,
        )


@pytest.mark.asyncio
async def test_a_page_is_handed_over_whole() -> None:
    """One page, one call. Ingesting a page a trade at a time costs a select,
    an insert and a commit each, which against a live tape is slower than the
    tape - and a store falling behind reports nothing: the counts keep rising
    while the newest trade stored keeps getting older."""
    stop = asyncio.Event()
    store = _Store(checkpoint=0)
    page = tuple(_row(index) for index in range(1, 251))
    rest = _Rest(page)

    await _poller(store, rest, _Sleeps(stop)).run(stop=stop)

    assert store.pages == 1
    assert len(store.seen) == 250


@pytest.mark.asyncio
async def test_an_empty_page_is_not_handed_over() -> None:
    """A quiet interval is not something for the store to open a transaction
    over."""
    stop = asyncio.Event()
    store = _Store(checkpoint=7)
    rest = _Rest(())

    await _poller(store, rest, _Sleeps(stop)).run(stop=stop)

    assert store.pages == 0


@pytest.mark.asyncio
async def test_an_instance_without_the_lease_writes_nothing() -> None:
    """The tape is one table shared by every instance on this database. Two
    pollers resuming from the same checkpoint fetch the same page and insert
    it twice, and the second one crashes on the unique id - which is the lucky
    outcome, because the unlucky one is a tape nobody can trust."""
    stop = asyncio.Event()
    store = _Store(checkpoint=1)
    rest = _Rest((_row(2),))
    lease = _Lease(held=False)

    poller = _poller(store, rest, _Sleeps(stop), lease)
    await poller.run(stop=stop)

    assert rest.asked == []
    assert store.seen == []
    assert store.pages == 0
    assert poller.deferred == 1


@pytest.mark.asyncio
async def test_losing_the_lease_is_not_an_error_and_does_not_end_the_run() -> None:
    """Another instance owning the account is a state to wait out, not a
    failure to back off from. The checkpoint says what was missed when
    leadership comes back."""
    stop = asyncio.Event()
    store = _Store(checkpoint=1)
    rest = _Rest((_row(2),))
    lease = _Lease(held=False)
    sleeps = _Sleeps(stop, stop_after=3)

    poller = _poller(store, rest, sleeps, lease)
    await poller.run(stop=stop)

    assert poller.failures == 0
    # The poll interval, not a growing backoff: this is not a failure.
    assert sleeps.waited == [POLL_INTERVAL_SECONDS] * 3


@pytest.mark.asyncio
async def test_leadership_regained_resumes_from_the_checkpoint() -> None:
    stop = asyncio.Event()
    store = _Store(checkpoint=41)
    rest = _Rest((_row(42),))
    lease = _Lease(held=False)
    sleeps = _Sleeps(stop, stop_after=2)

    async def take_leadership(seconds: float) -> None:
        lease.held = True
        await sleeps(seconds)

    poller = _poller(store, rest, take_leadership, lease)
    await poller.run(stop=stop)

    assert rest.asked == [42]
    assert store.seen == [42]


@pytest.mark.asyncio
async def test_leadership_is_asked_before_the_venue_is() -> None:
    """Asking the venue first would spend a request, and a rate-limit budget,
    on a page this instance is not allowed to store."""
    stop = asyncio.Event()
    lease = _Lease(held=False)

    poller = _poller(_Store(checkpoint=1), _Rest((_row(2),)), _Sleeps(stop), lease)
    await poller.run(stop=stop)

    assert lease.asked == 1
