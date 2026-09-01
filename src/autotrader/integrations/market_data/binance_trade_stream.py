"""The aggregate-trade stream, which nothing was running.

`BinanceUsdmMarketData.trade_prints` reads from a store; `ingest_agg_trade`
fills it and takes a websocket frame. Nothing called it outside the tests, so
in production the tape was always empty and every order-flow observation - Big
Trades, MIG, secado, the ceros, the thirty-second ATR, the extreme delta - had
no data at all.

The hard parts were already written. `ingest_agg_trade` deduplicates by
aggregate-trade id, recovers a gap over REST when ids skip, checks the
sequence before it stores anything, and advances the checkpoint. So this is
the small part: hold a connection, hand over each frame, and reconnect when
the connection drops. Whatever was missed while it was down is recovered by
the first frame after it, because the id will have skipped.

The one judgement here is which failures reconnect and which stop.

A connection that drops is ordinary - Binance closes them on its own daily -
and reconnecting is the whole point. An integrity failure is not: a correction
conflict means the venue is telling us something different about a trade we
have already stored, and a stream that swallowed it would let our record of
the tape diverge from the tape while continuing to look healthy. Those
propagate.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Protocol, cast

STREAM_URL = "wss://fstream.binance.com/ws/btcusdt@aggTrade"

# Doubling from a second, capped. Binance rate-limits reconnections, and a
# tight loop against a venue that is refusing connections is how an address
# gets banned rather than reconnected.
FIRST_BACKOFF_SECONDS = 1.0
MAXIMUM_BACKOFF_SECONDS = 60.0


class TradeIngest(Protocol):
    async def ingest_agg_trade(self, event: Mapping[str, object]) -> None: ...


class Frames(Protocol):
    """One connection, as an async iterator of text frames."""

    def __aiter__(self) -> AsyncIterator[str]: ...


Connect = Callable[[str], AbstractAsyncContextManager[Frames]]


class BinanceUsdmTradeStream:
    """Feed aggregate trades into the market data store until asked to stop."""

    def __init__(
        self,
        *,
        market_data: TradeIngest,
        connect: Connect,
        url: str = STREAM_URL,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        first_backoff: float = FIRST_BACKOFF_SECONDS,
        maximum_backoff: float = MAXIMUM_BACKOFF_SECONDS,
    ) -> None:
        if not url.startswith("wss://"):
            # Aggregate trades decide what the strategy believes about order
            # flow, and a cleartext feed is one anybody on the path can edit.
            raise ValueError("the trade stream must be wss://")
        self._market_data = market_data
        self._connect = connect
        self._url = url
        self._sleep = sleep
        self._first_backoff = first_backoff
        self._maximum_backoff = maximum_backoff
        self.frames = 0
        self.reconnects = 0

    async def run(self, *, stop: asyncio.Event) -> None:
        backoff = self._first_backoff
        while not stop.is_set():
            produced = False
            try:
                async with self._connect(self._url) as frames:
                    produced = await self._consume(frames, stop=stop)
            except TimeoutError, OSError, ConnectionError:
                # Ordinary. Binance closes connections daily on its own.
                pass
            if stop.is_set():
                return
            # A connection that ends without raising has still ended, and
            # reconnecting to it immediately is a hot loop against a venue
            # that just hung up. Every ending backs off; only whether it
            # produced anything decides how far.
            self.reconnects += 1
            await self._sleep(backoff)
            backoff = (
                self._first_backoff
                if produced
                else min(backoff * 2, self._maximum_backoff)
            )

    async def _consume(self, frames: Frames, *, stop: asyncio.Event) -> bool:
        """Whether this connection produced a trade, so the caller can tell a
        working connection that dropped from one that never worked."""
        produced = False
        async for frame in frames:
            if stop.is_set():
                return produced
            event = _decode(frame)
            if event is None:
                # Binance sends subscription acknowledgements on the same
                # socket. They are not trades and are not errors.
                continue
            await self._market_data.ingest_agg_trade(event)
            self.frames += 1
            produced = True
        return produced


def websockets_connect(url: str) -> AbstractAsyncContextManager[Frames]:
    """The real dialler, kept out of the stream so tests need no network.

    `websockets` answers pings on its own and raises its close exceptions from
    `OSError`, which is what the reconnect above already catches.
    """
    from websockets.asyncio.client import connect

    return cast(
        "AbstractAsyncContextManager[Frames]",
        connect(url, open_timeout=10, ping_interval=20, ping_timeout=20),
    )


def _decode(frame: str) -> Mapping[str, object] | None:
    """One frame, or None when it is not an aggregate trade.

    A frame that is not JSON at all is a broken connection rather than a
    broken trade, so it is left to the caller's reconnect.
    """
    try:
        payload = json.loads(frame)
    except ValueError as error:
        raise ConnectionError("the trade stream sent a frame that is not JSON") from (
            error
        )
    if not isinstance(payload, dict):
        raise ConnectionError("the trade stream sent a frame that is not an object")
    event = cast("dict[str, object]", payload)
    if event.get("e") != "aggTrade":
        return None
    return event


__all__ = (
    "FIRST_BACKOFF_SECONDS",
    "MAXIMUM_BACKOFF_SECONDS",
    "STREAM_URL",
    "BinanceUsdmTradeStream",
    "Connect",
    "Frames",
    "TradeIngest",
    "websockets_connect",
)
