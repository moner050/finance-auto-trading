from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Protocol, cast

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.strategies.david_v6.order_flow import TradePrint

_SYMBOL = "BTCUSDT"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_REST_LIMIT = 1500
_GAP_LIMIT = 1000

# Enough head of the tape to answer a correction without a round trip.
# Corrections arrive near the head; anything older the store answers for.
_TRADE_CACHE_SIZE = 10_000
_AGGREGATION_LOOKBACK = timedelta(days=1)
_TIMEFRAMES = {
    timedelta(minutes=1): "1m",
    timedelta(minutes=5): "5m",
    timedelta(minutes=15): "15m",
    timedelta(hours=1): "1h",
    timedelta(days=1): "1d",
}
_THIRTY_SECONDS = timedelta(seconds=30)
_FIVE_SECONDS = timedelta(seconds=5)


class BinanceUsdmMarketDataError(ValueError):
    """Raised when completed BTCUSDT evidence cannot be proven exactly."""


@dataclass(frozen=True, slots=True)
class BinanceUsdmMarketCheckpoint:
    symbol: str
    last_aggregate_trade_id: int
    last_trade_at: datetime

    def __post_init__(self) -> None:
        if self.symbol != _SYMBOL:
            raise ValueError("Binance USD-M market checkpoint requires BTCUSDT")
        if (
            type(self.last_aggregate_trade_id) is not int
            or self.last_aggregate_trade_id < 0
        ):
            raise ValueError("Binance USD-M market checkpoint ID is invalid")
        _require_utc(self.last_trade_at, "checkpoint last_trade_at")


class BinanceUsdmMarketRest(Protocol):
    async def klines(
        self,
        *,
        symbol: str,
        interval: str,
        end_time_ms: int,
        limit: int,
    ) -> tuple[object, ...]: ...

    async def aggregate_trades(
        self,
        *,
        symbol: str,
        from_id: int,
        limit: int,
    ) -> tuple[object, ...]: ...


class BinanceUsdmMarketStore(Protocol):
    async def load_checkpoint(
        self,
        symbol: str,
    ) -> BinanceUsdmMarketCheckpoint | None: ...

    async def find_trade(
        self,
        symbol: str,
        provider_trade_id: str,
    ) -> TradePrint | None: ...

    async def persist(
        self,
        symbol: str,
        trades: tuple[TradePrint, ...],
        checkpoint: BinanceUsdmMarketCheckpoint,
    ) -> None: ...

    async def load_trades(
        self,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[TradePrint, ...]: ...


class BinanceUsdmMarketData:
    """Completed BTCUSDT bars and deduplicated aggregate trade evidence."""

    def __init__(
        self,
        *,
        rest: BinanceUsdmMarketRest,
        store: BinanceUsdmMarketStore,
    ) -> None:
        self._rest = rest
        self._store = store
        self._checkpoint: BinanceUsdmMarketCheckpoint | None = None
        self._checkpoint_loaded = False
        self._trades: dict[int, TradePrint] = {}
        self._ingest_lock = asyncio.Lock()

    async def checkpoint_trade_id(self) -> int | None:
        """The last aggregate-trade id stored, or None when there is none.

        A poller resumes from the next one. None means the tape has never
        been read, which is a different thing from having read it and found
        nothing.
        """
        async with self._ingest_lock:
            await self._load_checkpoint()
            checkpoint = self._checkpoint
        return None if checkpoint is None else checkpoint.last_aggregate_trade_id

    async def ingest_agg_trade(self, event: Mapping[str, object]) -> None:
        """One aggregate trade as the websocket sends it."""
        await self._ingest(_decode_aggregate_trade(event, websocket=True))

    async def ingest_rest_agg_trade(self, row: Mapping[str, object]) -> None:
        """One aggregate trade as `/fapi/v1/aggTrades` returns it.

        The same trade, in the shape the REST endpoint uses: no event type and
        no symbol, because the request already named both. Decoding it as a
        websocket frame would mean synthesising those two fields onto it, and
        a decoder that has been handed a forged shape stops being able to
        refuse a real one.

        Everything after the decode is shared - the dedup, the gap recovery,
        the sequence check and the checkpoint all key on the aggregate-trade
        id, which is the same number either way.
        """
        await self.ingest_rest_agg_trades((row,))

    async def ingest_rest_agg_trades(
        self, rows: Sequence[Mapping[str, object]]
    ) -> None:
        """A whole page of aggregate trades, stored in one transaction.

        The same trades and the same refusals as ingesting them one at a
        time. What changes is the cost: a page arrives already contiguous and
        ordered, and putting each row back through a single-trade path made
        the store re-derive that one row at a time - a select, an insert and
        a commit each.

        Against a live tape that is slower than the tape itself, and a store
        that cannot keep up does not announce it. Every count still rises,
        the checkpoint still advances, and nothing reports an error; the only
        symptom is that the newest trade stored keeps getting older, so the
        window the strategy reads drifts into the past and eventually holds
        nothing at all.
        """
        await self._ingest_page(
            tuple(_decode_aggregate_trade(row, websocket=False) for row in rows)
        )

    async def _ingest(self, incoming: TradePrint) -> None:
        await self._ingest_page((incoming,))

    async def _ingest_page(self, incoming: tuple[TradePrint, ...]) -> None:
        """One or many, by the same rules.

        A single trade is a page of one, so the websocket path and the REST
        path cannot drift apart in what they refuse.
        """
        if not incoming:
            return
        async with self._ingest_lock:
            await self._load_checkpoint()
            checkpoint = self._checkpoint
            fresh: list[TradePrint] = []
            for trade in incoming:
                if await self._already_stored(trade, checkpoint=checkpoint):
                    continue
                fresh.append(trade)
            if not fresh:
                return

            recovered: tuple[TradePrint, ...] = ()
            first_id = _trade_id(fresh[0])
            if (
                checkpoint is not None
                and first_id > checkpoint.last_aggregate_trade_id + 1
            ):
                recovered = await self._recover_gap(
                    checkpoint.last_aggregate_trade_id + 1,
                    first_id,
                )
            new_trades = (*recovered, *fresh)
            _require_trade_sequence(new_trades, after=checkpoint)
            last = new_trades[-1]
            next_checkpoint = BinanceUsdmMarketCheckpoint(
                symbol=_SYMBOL,
                last_aggregate_trade_id=_trade_id(last),
                last_trade_at=last.occurred_at,
            )
            await self._store.persist(_SYMBOL, new_trades, next_checkpoint)
            self._checkpoint = next_checkpoint
            self._remember(new_trades)

    async def _already_stored(
        self,
        incoming: TradePrint,
        *,
        checkpoint: BinanceUsdmMarketCheckpoint | None,
    ) -> bool:
        """Whether this trade is one we hold, refusing a changed one.

        The venue re-sending a trade is ordinary. The venue re-sending it
        with different contents is not: it means our record of the tape has
        stopped matching the tape, and storing either version would leave
        nothing able to tell which one the strategy decided against.
        """
        incoming_id = _trade_id(incoming)
        cached = self._trades.get(incoming_id)
        if cached is not None:
            if cached != incoming:
                raise BinanceUsdmMarketDataError(
                    "Binance USD-M aggregate trade correction conflict"
                )
            return True
        if checkpoint is None or incoming_id > checkpoint.last_aggregate_trade_id:
            return False
        persisted = await self._store.find_trade(_SYMBOL, incoming.provider_trade_id)
        if persisted is not None and persisted != incoming:
            raise BinanceUsdmMarketDataError(
                "Binance USD-M aggregate trade correction conflict"
            )
        return True

    def _remember(self, trades: tuple[TradePrint, ...]) -> None:
        """Keep the most recent ids, and only those.

        The cache exists to catch a correction without a round trip, and a
        correction arrives near the tape's head. Keeping every trade a
        session ever saw would grow without bound over the hours a Shadow
        session runs, to answer for ids the store already answers for.
        """
        self._trades.update({_trade_id(trade): trade for trade in trades})
        excess = len(self._trades) - _TRADE_CACHE_SIZE
        if excess <= 0:
            return
        for trade_id in sorted(self._trades)[:excess]:
            del self._trades[trade_id]

    async def completed_bars(
        self,
        timeframe: timedelta,
        end_at: datetime,
    ) -> tuple[CompletedOhlcvBar, ...]:
        end_at = _require_utc(end_at, "completed bar end_at")
        if timeframe == _THIRTY_SECONDS:
            return await self._aggregate_bars(_THIRTY_SECONDS, end_at)
        interval = _TIMEFRAMES.get(timeframe)
        if interval is None:
            raise ValueError(
                "Binance USD-M completed bars require a supported timeframe"
            )
        rows = await self._rest.klines(
            symbol=_SYMBOL,
            interval=interval,
            end_time_ms=_epoch_ms(end_at),
            limit=_REST_LIMIT,
        )
        return _compile_klines(rows, timeframe=timeframe, end_at=end_at)

    async def telemetry_bars(
        self,
        end_at: datetime,
    ) -> tuple[CompletedOhlcvBar, ...]:
        return await self._aggregate_bars(
            _FIVE_SECONDS,
            _require_utc(end_at, "telemetry end_at"),
        )

    async def trade_prints(
        self,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[TradePrint, ...]:
        start_at = _require_utc(start_at, "trade start_at")
        end_at = _require_utc(end_at, "trade end_at")
        if start_at >= end_at:
            raise ValueError("Binance USD-M trade range is invalid")
        persisted = await self._store.load_trades(_SYMBOL, start_at, end_at)
        if type(persisted) is not tuple or any(
            type(trade) is not TradePrint for trade in persisted
        ):
            raise BinanceUsdmMarketDataError(
                "Binance USD-M persisted trades are invalid"
            )
        by_id: dict[str, TradePrint] = {}
        for trade in (*persisted, *self._trades.values()):
            if not start_at <= trade.occurred_at < end_at:
                continue
            previous = by_id.get(trade.provider_trade_id)
            if previous is not None and previous != trade:
                raise BinanceUsdmMarketDataError(
                    "Binance USD-M aggregate trade correction conflict"
                )
            by_id[trade.provider_trade_id] = trade
        return tuple(
            sorted(
                by_id.values(),
                key=lambda trade: (trade.occurred_at, _trade_id(trade)),
            )
        )

    async def _load_checkpoint(self) -> None:
        if self._checkpoint_loaded:
            return
        checkpoint = await self._store.load_checkpoint(_SYMBOL)
        if (
            checkpoint is not None
            and type(checkpoint) is not BinanceUsdmMarketCheckpoint
        ):
            raise BinanceUsdmMarketDataError(
                "Binance USD-M persisted checkpoint is invalid"
            )
        self._checkpoint = checkpoint
        self._checkpoint_loaded = True

    async def _recover_gap(
        self,
        first_missing_id: int,
        incoming_id: int,
    ) -> tuple[TradePrint, ...]:
        result: list[TradePrint] = []
        next_id = first_missing_id
        while next_id < incoming_id:
            limit = min(_GAP_LIMIT, incoming_id - next_id)
            rows = await self._rest.aggregate_trades(
                symbol=_SYMBOL,
                from_id=next_id,
                limit=limit,
            )
            if type(rows) is not tuple or not rows:
                raise BinanceUsdmMarketDataError(
                    "Binance USD-M aggregate trade gap is unresolved"
                )
            page = tuple(
                _decode_aggregate_trade(_object(row), websocket=False) for row in rows
            )
            for trade in page:
                trade_id = _trade_id(trade)
                if trade_id != next_id or trade_id >= incoming_id:
                    raise BinanceUsdmMarketDataError(
                        "Binance USD-M aggregate trade gap is unresolved"
                    )
                result.append(trade)
                next_id += 1
        return tuple(result)

    async def _aggregate_bars(
        self,
        duration: timedelta,
        end_at: datetime,
    ) -> tuple[CompletedOhlcvBar, ...]:
        trades = await self.trade_prints(
            end_at - _AGGREGATION_LOOKBACK,
            end_at + timedelta(milliseconds=1),
        )
        if not trades:
            return ()
        watermark = min(end_at, max(trade.occurred_at for trade in trades))
        duration_ms = _duration_ms(duration)
        buckets: dict[int, list[TradePrint]] = {}
        for trade in trades:
            opened_ms = (_epoch_ms(trade.occurred_at) // duration_ms) * duration_ms
            buckets.setdefault(opened_ms, []).append(trade)
        result: list[CompletedOhlcvBar] = []
        for opened_ms, values in sorted(buckets.items()):
            completed_at = _EPOCH + timedelta(milliseconds=opened_ms + duration_ms)
            if completed_at > watermark:
                continue
            ordered = sorted(
                values,
                key=lambda trade: (trade.occurred_at, _trade_id(trade)),
            )
            prices = [
                _required_decimal(trade.price, "trade price") for trade in ordered
            ]
            quantities = [
                _required_decimal(trade.quantity, "trade quantity") for trade in ordered
            ]
            result.append(
                CompletedOhlcvBar(
                    timestamp=completed_at,
                    open=prices[0],
                    high=max(prices),
                    low=min(prices),
                    close=prices[-1],
                    volume=sum(quantities, start=Decimal()),
                )
            )
        return tuple(result)


def _compile_klines(
    rows: tuple[object, ...],
    *,
    timeframe: timedelta,
    end_at: datetime,
) -> tuple[CompletedOhlcvBar, ...]:
    if type(rows) is not tuple:
        raise BinanceUsdmMarketDataError("Binance USD-M kline response is invalid")
    duration_ms = _duration_ms(timeframe)
    by_open: dict[int, CompletedOhlcvBar] = {}
    for raw in rows:
        row = _sequence(raw)
        if len(row) != 12:
            raise BinanceUsdmMarketDataError("Binance USD-M kline response is invalid")
        opened_ms = _integer(row[0], "kline open time")
        closed_ms = _integer(row[6], "kline close time")
        if opened_ms % duration_ms or closed_ms != opened_ms + duration_ms - 1:
            raise BinanceUsdmMarketDataError("Binance USD-M kline boundary is invalid")
        bar = CompletedOhlcvBar(
            timestamp=_EPOCH + timedelta(milliseconds=opened_ms + duration_ms),
            open=_decimal(row[1], "kline open"),
            high=_decimal(row[2], "kline high"),
            low=_decimal(row[3], "kline low"),
            close=_decimal(row[4], "kline close"),
            volume=_decimal(row[5], "kline volume"),
        )
        previous = by_open.get(opened_ms)
        if previous is not None and previous != bar:
            raise BinanceUsdmMarketDataError("Binance USD-M kline correction conflict")
        by_open[opened_ms] = bar
    opened = sorted(by_open)
    if any(right - left != duration_ms for left, right in pairwise(opened)):
        raise BinanceUsdmMarketDataError("Binance USD-M kline gap is unresolved")
    return tuple(
        by_open[value] for value in opened if by_open[value].timestamp <= end_at
    )


def _decode_aggregate_trade(
    value: Mapping[str, object],
    *,
    websocket: bool,
) -> TradePrint:
    if websocket and (value.get("e") != "aggTrade" or value.get("s") != _SYMBOL):
        raise BinanceUsdmMarketDataError(
            "Binance USD-M websocket aggregate trade requires BTCUSDT"
        )
    if not websocket and "s" in value and value.get("s") != _SYMBOL:
        raise BinanceUsdmMarketDataError(
            "Binance USD-M REST aggregate trade requires BTCUSDT"
        )
    trade_id = _integer(value.get("a"), "aggregate trade ID")
    first_id = _integer(value.get("f"), "first trade ID")
    last_id = _integer(value.get("l"), "last trade ID")
    occurred_ms = _integer(value.get("T"), "aggregate trade time")
    buyer_maker = value.get("m")
    if first_id > last_id or type(buyer_maker) is not bool:
        raise BinanceUsdmMarketDataError("Binance USD-M aggregate trade is invalid")
    return TradePrint(
        provider_trade_id=str(trade_id),
        occurred_at=_EPOCH + timedelta(milliseconds=occurred_ms),
        price=_decimal(value.get("p"), "aggregate trade price"),
        quantity=_decimal(value.get("q"), "aggregate trade quantity"),
        buyer_maker=buyer_maker,
    )


def _require_trade_sequence(
    trades: tuple[TradePrint, ...],
    *,
    after: BinanceUsdmMarketCheckpoint | None,
) -> None:
    expected = None if after is None else after.last_aggregate_trade_id + 1
    previous_at = None if after is None else after.last_trade_at
    for trade in trades:
        trade_id = _trade_id(trade)
        if expected is not None and trade_id != expected:
            raise BinanceUsdmMarketDataError(
                "Binance USD-M aggregate trade gap is unresolved"
            )
        if previous_at is not None and trade.occurred_at < previous_at:
            raise BinanceUsdmMarketDataError(
                "Binance USD-M aggregate trade order is invalid"
            )
        expected = trade_id + 1
        previous_at = trade.occurred_at


def _trade_id(trade: TradePrint) -> int:
    value = trade.provider_trade_id
    if not value.isascii() or not value.isdecimal():
        raise BinanceUsdmMarketDataError("Binance USD-M aggregate trade ID is invalid")
    return int(value)


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BinanceUsdmMarketDataError("Binance USD-M aggregate trade is invalid")
    raw = cast(Mapping[object, object], value)
    if any(type(key) is not str for key in raw):
        raise BinanceUsdmMarketDataError("Binance USD-M aggregate trade is invalid")
    return cast(Mapping[str, object], raw)


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise BinanceUsdmMarketDataError("Binance USD-M kline response is invalid")
    return cast(Sequence[object], value)


def _integer(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise BinanceUsdmMarketDataError(f"Binance USD-M {name} is invalid")
    return value


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is not str or not value:
        raise BinanceUsdmMarketDataError(f"Binance USD-M {name} is invalid")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise BinanceUsdmMarketDataError(f"Binance USD-M {name} is invalid") from error
    if not parsed.is_finite():
        raise BinanceUsdmMarketDataError(f"Binance USD-M {name} is invalid")
    return parsed


def _required_decimal(value: Decimal | None, name: str) -> Decimal:
    if type(value) is not Decimal:
        raise BinanceUsdmMarketDataError(f"Binance USD-M {name} is unavailable")
    return value


def _duration_ms(value: timedelta) -> int:
    milliseconds = value // timedelta(milliseconds=1)
    if value != timedelta(milliseconds=milliseconds) or milliseconds <= 0:
        raise ValueError("Binance USD-M timeframe is invalid")
    return milliseconds


def _epoch_ms(value: datetime) -> int:
    delta = value - _EPOCH
    milliseconds = delta // timedelta(milliseconds=1)
    if delta != timedelta(milliseconds=milliseconds) or milliseconds < 0:
        raise ValueError("Binance USD-M timestamp must use exact milliseconds")
    return milliseconds


def _require_utc(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is not UTC
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"Binance USD-M {name} must use exact UTC")
    return value


__all__ = (
    "BinanceUsdmMarketCheckpoint",
    "BinanceUsdmMarketData",
    "BinanceUsdmMarketDataError",
    "BinanceUsdmMarketRest",
    "BinanceUsdmMarketStore",
)
