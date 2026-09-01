"""The aggregate-trade tape over REST, for when the stream will not deliver.

`btcusdt@aggTrade` accepts a subscription from this host and then sends
nothing, while `btcusdt@trade` on the same socket delivers and
`/fapi/v1/aggTrades` returns live aggregate trades with every field the
decoder wants. So the tape is fetched rather than pushed.

This is the same data, not a substitute for it. Raw trades were the other
option - section 22.4 allows either - but every piece of machinery already
built keys on the aggregate-trade id: the deduplication, the
`last_aggregate_trade_id` checkpoint, and the gap recovery which fetches from
this very endpoint. Switching id space would have been a rewrite of all three;
polling changes only where the same rows arrive from.

What it costs is latency. A poll every few seconds is behind a stream by up to
that interval, which the strategy can afford: it evaluates on five-minute bars
and reads a thirty-minute trade window. What it must not do is fetch faster
than the venue's weight budget allows, which is why the interval is stated and
not tuned down to feel live.

Ordering is not this module's problem. `ingest_rest_agg_trades` refuses a trade
that arrives out of sequence, recovers a gap when ids skip, and advances the
checkpoint - the same path the stream used. The page goes over whole, because
a page stored one commit at a time is slower than the tape that produced it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol, cast

# A poll behind by this much is invisible to a five-minute bar, and each one
# costs weight against a budget shared with everything else this system reads.
POLL_INTERVAL_SECONDS = 2.0

# The venue's page size for this endpoint. Asking for more is refused; asking
# for less makes a busy minute take several polls to catch up.
PAGE_LIMIT = 1000

FIRST_BACKOFF_SECONDS = 1.0
MAXIMUM_BACKOFF_SECONDS = 60.0


class AggregateTradeRest(Protocol):
    async def aggregate_trades(
        self, *, symbol: str, from_id: int | None, limit: int
    ) -> tuple[object, ...]: ...


class RestTradeIngest(Protocol):
    async def ingest_rest_agg_trades(
        self, rows: Sequence[Mapping[str, object]]
    ) -> None: ...

    async def checkpoint_trade_id(self) -> int | None: ...


class BinanceUsdmTradePoller:
    """Fetch aggregate trades into the store until asked to stop."""

    def __init__(
        self,
        *,
        market_data: RestTradeIngest,
        rest: AggregateTradeRest,
        symbol: str = "BTCUSDT",
        interval: float = POLL_INTERVAL_SECONDS,
        limit: int = PAGE_LIMIT,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        first_backoff: float = FIRST_BACKOFF_SECONDS,
        maximum_backoff: float = MAXIMUM_BACKOFF_SECONDS,
    ) -> None:
        if interval <= 0:
            raise ValueError("the poll interval must be positive")
        if not 0 < limit <= PAGE_LIMIT:
            raise ValueError(f"the page limit must be within 1 and {PAGE_LIMIT}")
        self._market_data = market_data
        self._rest = rest
        self._symbol = symbol
        self._interval = interval
        self._limit = limit
        self._sleep = sleep
        self._first_backoff = first_backoff
        self._maximum_backoff = maximum_backoff
        self.trades = 0
        self.polls = 0
        self.failures = 0

    async def run(self, *, stop: asyncio.Event) -> None:
        backoff = self._first_backoff
        while not stop.is_set():
            try:
                fetched = await self._poll()
            except OSError, TimeoutError, ConnectionError:
                # Reaching the venue is allowed to fail. What it sends, once
                # it arrives, is not - an out-of-sequence trade or a
                # correction conflict propagates from `_poll`.
                if stop.is_set():
                    return
                self.failures += 1
                await self._sleep(backoff)
                backoff = min(backoff * 2, self._maximum_backoff)
                continue
            backoff = self._first_backoff
            if stop.is_set():
                return
            # A full page means the tape has more waiting, so the next fetch
            # goes out immediately rather than idling behind a backlog.
            if fetched < self._limit:
                await self._sleep(self._interval)

    async def _poll(self) -> int:
        checkpoint = await self._market_data.checkpoint_trade_id()
        # From the next unseen id, or from the most recent page when the tape
        # has never been read. Not from zero: that is a real id at the start
        # of the venue's history, and the first poll would begin in 2019.
        from_id = None if checkpoint is None else checkpoint + 1
        rows = await self._rest.aggregate_trades(
            symbol=self._symbol, from_id=from_id, limit=self._limit
        )
        self.polls += 1
        page = _rows(rows)
        if not page:
            # A quiet interval is not something to open a transaction over.
            return 0
        await self._market_data.ingest_rest_agg_trades(page)
        self.trades += len(page)
        return len(rows)


def _rows(rows: Sequence[object]) -> tuple[Mapping[str, object], ...]:
    if any(not isinstance(row, dict) for row in rows):
        raise ConnectionError("the aggregate trade page is not a list of objects")
    return tuple(cast("Mapping[str, object]", row) for row in rows)


__all__ = (
    "FIRST_BACKOFF_SECONDS",
    "MAXIMUM_BACKOFF_SECONDS",
    "PAGE_LIMIT",
    "POLL_INTERVAL_SECONDS",
    "AggregateTradeRest",
    "BinanceUsdmTradePoller",
    "RestTradeIngest",
)
