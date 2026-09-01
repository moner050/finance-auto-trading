"""Which failures reconnect, and which must not.

The stream feeds every order-flow observation the strategy has. A connection
that drops is ordinary and reconnecting is the point; a correction conflict
means the venue is telling us something different about a trade we already
stored, and swallowing it would let our record of the tape diverge from the
tape while the stream went on looking healthy.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping

import pytest

from autotrader.integrations.market_data.binance_trade_stream import (
    MAXIMUM_BACKOFF_SECONDS,
    STREAM_URL,
    BinanceUsdmTradeStream,
)
from autotrader.integrations.market_data.binance_usdm import (
    BinanceUsdmMarketDataError,
)


def _trade(trade_id: int) -> str:
    return json.dumps(
        {
            "e": "aggTrade",
            "s": "BTCUSDT",
            "a": trade_id,
            "f": trade_id,
            "l": trade_id,
            "T": 1_800_000_000_000 + trade_id,
            "p": "70000.0",
            "q": "0.01",
            "m": False,
        }
    )


class _Ingest:
    def __init__(self, *, fail_on: int | None = None) -> None:
        self.seen: list[int] = []
        self._fail_on = fail_on

    async def ingest_agg_trade(self, event: Mapping[str, object]) -> None:
        trade_id = int(str(event["a"]))
        if trade_id == self._fail_on:
            raise BinanceUsdmMarketDataError(
                "Binance USD-M aggregate trade correction conflict"
            )
        self.seen.append(trade_id)


class _Connection:
    """One connection, yielding frames then ending however it was told to."""

    def __init__(self, frames: tuple[str, ...], *, then: Exception | None) -> None:
        self._frames = frames
        self._then = then

    async def __aenter__(self) -> _Connection:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def __aiter__(self) -> AsyncIterator[str]:
        for frame in self._frames:
            yield frame
        if self._then is not None:
            raise self._then


class _Dialler:
    def __init__(self, *connections: _Connection) -> None:
        self._connections = list(connections)
        self.calls = 0

    def __call__(self, url: str) -> _Connection:
        del url
        self.calls += 1
        if self._connections:
            return self._connections.pop(0)
        return _Connection((), then=None)


class _Sleeps:
    def __init__(self, stop: asyncio.Event, *, stop_after: int = 3) -> None:
        self.waited: list[float] = []
        self._stop = stop
        self._stop_after = stop_after

    async def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)
        if len(self.waited) >= self._stop_after:
            self._stop.set()


def _stream(
    dialler: _Dialler, ingest: _Ingest, sleeps: object
) -> BinanceUsdmTradeStream:
    return BinanceUsdmTradeStream(
        market_data=ingest,
        connect=dialler,  # type: ignore[arg-type]
        sleep=sleeps,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_every_aggregate_trade_reaches_the_store() -> None:
    stop = asyncio.Event()
    ingest = _Ingest()
    dialler = _Dialler(_Connection((_trade(1), _trade(2)), then=None))
    sleeps = _Sleeps(stop, stop_after=1)

    await _stream(dialler, ingest, sleeps).run(stop=stop)

    assert ingest.seen == [1, 2]


@pytest.mark.asyncio
async def test_a_frame_that_is_not_a_trade_is_skipped() -> None:
    """Binance sends subscription acknowledgements on the same socket. They
    are not trades and they are not errors."""
    stop = asyncio.Event()
    ingest = _Ingest()
    acknowledgement = json.dumps({"result": None, "id": 1})
    dialler = _Dialler(_Connection((acknowledgement, _trade(1)), then=None))
    sleeps = _Sleeps(stop, stop_after=1)

    await _stream(dialler, ingest, sleeps).run(stop=stop)

    assert ingest.seen == [1]


@pytest.mark.asyncio
async def test_a_dropped_connection_is_reconnected() -> None:
    """Binance closes them daily on its own. Whatever was missed is recovered
    by the first frame after, because the aggregate-trade id will have
    skipped."""
    stop = asyncio.Event()
    ingest = _Ingest()
    dialler = _Dialler(
        _Connection((_trade(1),), then=ConnectionError("closed")),
        _Connection((_trade(2),), then=None),
    )
    sleeps = _Sleeps(stop, stop_after=2)
    stream = _stream(dialler, ingest, sleeps)

    await stream.run(stop=stop)

    assert ingest.seen == [1, 2]
    assert stream.reconnects >= 1
    assert dialler.calls >= 2


@pytest.mark.asyncio
async def test_the_backoff_grows_and_is_capped() -> None:
    """A tight loop against a venue refusing connections is how an address
    gets banned rather than reconnected."""
    stop = asyncio.Event()
    ingest = _Ingest()
    dialler = _Dialler(
        *(_Connection((), then=ConnectionError("refused")) for _ in range(8))
    )
    sleeps = _Sleeps(stop, stop_after=8)

    await _stream(dialler, ingest, sleeps).run(stop=stop)

    assert sleeps.waited[0] == 1.0
    assert sleeps.waited[1] == 2.0
    assert all(value <= MAXIMUM_BACKOFF_SECONDS for value in sleeps.waited)
    assert sleeps.waited == sorted(sleeps.waited)


@pytest.mark.asyncio
async def test_a_correction_conflict_stops_the_stream() -> None:
    """The venue disagreeing with a trade we already stored is not something
    to reconnect through. Continuing would keep the record diverging from the
    tape while the stream went on looking healthy."""
    stop = asyncio.Event()
    ingest = _Ingest(fail_on=2)
    dialler = _Dialler(_Connection((_trade(1), _trade(2)), then=None))
    sleeps = _Sleeps(stop, stop_after=1)

    with pytest.raises(BinanceUsdmMarketDataError, match="correction conflict"):
        await _stream(dialler, ingest, sleeps).run(stop=stop)

    assert ingest.seen == [1]


@pytest.mark.asyncio
async def test_stopping_ends_the_run_without_reconnecting() -> None:
    stop = asyncio.Event()
    stop.set()
    ingest = _Ingest()
    dialler = _Dialler(_Connection((_trade(1),), then=None))

    await _stream(dialler, ingest, _Sleeps(stop)).run(stop=stop)

    assert ingest.seen == []
    assert dialler.calls == 0


def test_a_cleartext_feed_is_refused() -> None:
    """Aggregate trades decide what the strategy believes about order flow,
    and a cleartext feed is one anybody on the path can edit."""
    with pytest.raises(ValueError, match="wss://"):
        BinanceUsdmTradeStream(
            market_data=_Ingest(),
            connect=_Dialler(),  # type: ignore[arg-type]
            url="ws://fstream.binance.com/ws/btcusdt@aggTrade",
        )


def test_the_default_stream_is_the_usd_m_aggregate_trade_feed() -> None:
    assert STREAM_URL == "wss://fstream.binance.com/ws/btcusdt@aggTrade"
