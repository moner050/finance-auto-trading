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

    async def ingest_agg_trade(self, event: Mapping[str, object]) -> None:
        incoming = _decode_aggregate_trade(event, websocket=True)
        incoming_id = _trade_id(incoming)
        async with self._ingest_lock:
            await self._load_checkpoint()
            cached = self._trades.get(incoming_id)
            if cached is not None:
                if cached != incoming:
                    raise BinanceUsdmMarketDataError(
                        "Binance USD-M aggregate trade correction conflict"
                    )
                return
            checkpoint = self._checkpoint
            if (
                checkpoint is not None
                and incoming_id <= checkpoint.last_aggregate_trade_id
            ):
                persisted = await self._store.find_trade(
                    _SYMBOL,
                    incoming.provider_trade_id,
                )
                if persisted is not None and persisted != incoming:
                    raise BinanceUsdmMarketDataError(
                        "Binance USD-M aggregate trade correction conflict"
                    )
                return

            recovered: tuple[TradePrint, ...] = ()
            if (
                checkpoint is not None
                and incoming_id > checkpoint.last_aggregate_trade_id + 1
            ):
                recovered = await self._recover_gap(
                    checkpoint.last_aggregate_trade_id + 1,
                    incoming_id,
                )
            new_trades = (*recovered, incoming)
            _require_trade_sequence(new_trades, after=checkpoint)
            next_checkpoint = BinanceUsdmMarketCheckpoint(
                symbol=_SYMBOL,
                last_aggregate_trade_id=incoming_id,
                last_trade_at=incoming.occurred_at,
            )
            await self._store.persist(_SYMBOL, new_trades, next_checkpoint)
            self._checkpoint = next_checkpoint
            self._trades.update({_trade_id(trade): trade for trade in new_trades})

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
