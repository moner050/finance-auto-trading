from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast
from urllib.parse import urlencode

from autotrader.integrations.brokers.common import BrokerRequest, BrokerResponse

_SYMBOL = "BTCUSDT"
_WINDOW = timedelta(days=7)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class BinanceUsdmAccountCaptureError(RuntimeError):
    """Raised without provider payload when an exact snapshot is unavailable."""


class BinanceUsdmAccountReader(Protocol):
    async def send(self, request: BrokerRequest) -> BrokerResponse: ...


@dataclass(frozen=True, slots=True)
class BinanceUsdmBalance:
    asset: str
    balance: Decimal
    available_balance: Decimal
    maximum_withdraw_amount: Decimal
    updated_at: datetime

    def __post_init__(self) -> None:
        _asset(self.asset)
        for name in ("balance", "available_balance", "maximum_withdraw_amount"):
            value = _exact_decimal(getattr(self, name), name)
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        _require_utc(self.updated_at, "balance updated_at")


@dataclass(frozen=True, slots=True)
class BinanceUsdmPosition:
    symbol: str
    position_side: str
    amount: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    isolated_margin: Decimal
    notional: Decimal
    margin_asset: str
    initial_margin: Decimal
    maintenance_margin: Decimal
    position_initial_margin: Decimal
    open_order_initial_margin: Decimal
    updated_at: datetime

    def __post_init__(self) -> None:
        _symbol(self.symbol)
        if self.position_side not in {"BOTH", "LONG", "SHORT"}:
            raise ValueError("position_side is invalid")
        _asset(self.margin_asset)
        for name in (
            "amount",
            "entry_price",
            "mark_price",
            "unrealized_pnl",
            "isolated_margin",
            "notional",
            "initial_margin",
            "maintenance_margin",
            "position_initial_margin",
            "open_order_initial_margin",
        ):
            _exact_decimal(getattr(self, name), name)
        if self.entry_price < 0 or self.mark_price < 0:
            raise ValueError("position prices must be non-negative")
        if any(
            value < 0
            for value in (
                self.isolated_margin,
                self.initial_margin,
                self.maintenance_margin,
                self.position_initial_margin,
                self.open_order_initial_margin,
            )
        ):
            raise ValueError("position margins must be non-negative")
        _require_utc(self.updated_at, "position updated_at")


@dataclass(frozen=True, slots=True)
class BinanceUsdmOpenOrder:
    order_id: int
    client_order_id: str
    symbol: str
    status: str
    side: str
    order_type: str
    executed_quantity: Decimal
    original_quantity: Decimal
    reduce_only: bool
    close_position: bool


@dataclass(frozen=True, slots=True)
class BinanceUsdmOpenAlgoOrder:
    algo_id: int
    client_algo_id: str
    symbol: str
    status: str
    side: str
    order_type: str
    quantity: Decimal
    trigger_price: Decimal
    close_position: bool


@dataclass(frozen=True, slots=True)
class BinanceUsdmTradeFact:
    trade_id: int
    order_id: int
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    commission: Decimal
    commission_asset: str
    realized_pnl: Decimal
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class BinanceUsdmIncomeFact:
    transaction_id: int
    trade_id: str
    symbol: str
    income_type: str
    income: Decimal
    asset: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class BinanceUsdmAccountSnapshot:
    as_of: datetime
    balances: tuple[BinanceUsdmBalance, ...]
    positions: tuple[BinanceUsdmPosition, ...]
    normal_orders: tuple[BinanceUsdmOpenOrder, ...]
    algo_orders: tuple[BinanceUsdmOpenAlgoOrder, ...]
    trades: tuple[BinanceUsdmTradeFact, ...]
    income: tuple[BinanceUsdmIncomeFact, ...]

    def __post_init__(self) -> None:
        _require_utc(self.as_of, "account snapshot as_of")
        collections: tuple[tuple[tuple[object, ...], type[object], str], ...] = (
            (self.balances, BinanceUsdmBalance, "balances"),
            (self.positions, BinanceUsdmPosition, "positions"),
            (self.normal_orders, BinanceUsdmOpenOrder, "normal_orders"),
            (self.algo_orders, BinanceUsdmOpenAlgoOrder, "algo_orders"),
            (self.trades, BinanceUsdmTradeFact, "trades"),
            (self.income, BinanceUsdmIncomeFact, "income"),
        )
        for values, expected, name in collections:
            if type(values) is not tuple or any(
                type(value) is not expected for value in values
            ):
                raise ValueError(f"account snapshot {name} is invalid")
        if sum(balance.asset == "USDT" for balance in self.balances) != 1:
            raise ValueError("account snapshot requires exactly one USDT balance")

    @property
    def usdt_wallet_balance(self) -> Decimal:
        return self._usdt.balance

    @property
    def usdt_available_balance(self) -> Decimal:
        return self._usdt.available_balance

    @property
    def usdt_equity(self) -> Decimal:
        return self.usdt_wallet_balance + sum(
            (
                position.unrealized_pnl
                for position in self.positions
                if position.margin_asset == "USDT"
            ),
            start=Decimal(),
        )

    @property
    def initial_margin(self) -> Decimal:
        return sum(
            (
                position.position_initial_margin + position.open_order_initial_margin
                for position in self.positions
            ),
            start=Decimal(),
        )

    @property
    def maintenance_margin(self) -> Decimal:
        return sum(
            (position.maintenance_margin for position in self.positions),
            start=Decimal(),
        )

    @property
    def _usdt(self) -> BinanceUsdmBalance:
        return next(balance for balance in self.balances if balance.asset == "USDT")


async def capture_binance_usdm_account(
    *,
    reader: BinanceUsdmAccountReader,
    as_of: datetime,
) -> BinanceUsdmAccountSnapshot:
    as_of = _truncate_to_milliseconds(_require_utc(as_of, "account capture as_of"))
    start_at = as_of - _WINDOW
    history_query = urlencode(
        (
            ("startTime", str(_epoch_ms(start_at))),
            ("endTime", str(_epoch_ms(as_of))),
            ("limit", "1000"),
        )
    )
    symbol_history_query = f"symbol={_SYMBOL}&{history_query}"
    requests = (
        BrokerRequest(method="GET", path="/fapi/v3/balance"),
        BrokerRequest(method="GET", path="/fapi/v3/positionRisk"),
        BrokerRequest(method="GET", path="/fapi/v1/openOrders"),
        BrokerRequest(method="GET", path="/fapi/v1/openAlgoOrders"),
        BrokerRequest(
            method="GET",
            path=f"/fapi/v1/allOrders?{symbol_history_query}",
        ),
        BrokerRequest(
            method="GET",
            path=f"/fapi/v1/allAlgoOrders?{symbol_history_query}",
        ),
        BrokerRequest(
            method="GET",
            path=f"/fapi/v1/userTrades?{symbol_history_query}",
        ),
        BrokerRequest(method="GET", path=f"/fapi/v1/income?{history_query}"),
    )
    try:
        responses = tuple([await reader.send(request) for request in requests])
        payloads = tuple(_payload(response) for response in responses)
        arrays = tuple(_array(payload) for payload in payloads)
        if any(len(values) >= 1000 for values in arrays[4:]):
            raise ValueError("Binance USD-M history may be truncated")
        balances = tuple(_balance(value) for value in arrays[0])
        positions = tuple(_position(value) for value in arrays[1])
        open_normal_orders = tuple(_normal_order(value) for value in arrays[2])
        open_algo_orders = tuple(_algo_order(value) for value in arrays[3])
        normal_order_history = tuple(_normal_order(value) for value in arrays[4])
        algo_order_history = tuple(_algo_order(value) for value in arrays[5])
        normal_orders = _merge_exact(
            open_normal_orders,
            normal_order_history,
            identity=lambda order: order.order_id,
        )
        algo_orders = _merge_exact(
            open_algo_orders,
            algo_order_history,
            identity=lambda order: order.algo_id,
        )
        trades = tuple(_trade(value) for value in arrays[6])
        income = tuple(_income(value) for value in arrays[7])
        _unique((balance.asset for balance in balances), "balance asset")
        _unique((position.symbol for position in positions), "position symbol")
        _unique((order.order_id for order in normal_orders), "normal order ID")
        _unique(
            (order.client_order_id for order in normal_orders),
            "normal client order ID",
        )
        _unique((order.algo_id for order in algo_orders), "algo order ID")
        _unique(
            (order.client_algo_id for order in algo_orders),
            "algo client order ID",
        )
        _unique((trade.trade_id for trade in trades), "trade ID")
        _unique((fact.transaction_id for fact in income), "income transaction ID")
        return BinanceUsdmAccountSnapshot(
            as_of=as_of,
            balances=balances,
            positions=positions,
            normal_orders=normal_orders,
            algo_orders=algo_orders,
            trades=trades,
            income=income,
        )
    except asyncio.CancelledError, KeyboardInterrupt, SystemExit:
        raise
    except Exception:
        raise BinanceUsdmAccountCaptureError(
            "Binance USD-M account snapshot is incomplete"
        ) from None


def _payload(response: BrokerResponse) -> object:
    if type(response) is not BrokerResponse or response.status != 200:
        raise ValueError
    try:
        return cast(object, json.loads(response.body))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError from error


def _array(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise ValueError
    return tuple(_object(item) for item in cast(list[object], value))


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError
    raw = cast(dict[object, object], value)
    if any(type(key) is not str for key in raw):
        raise ValueError
    return cast(dict[str, object], raw)


def _balance(value: dict[str, object]) -> BinanceUsdmBalance:
    return BinanceUsdmBalance(
        asset=_text(value.get("asset")),
        balance=_provider_decimal(value.get("balance")),
        available_balance=_provider_decimal(value.get("availableBalance")),
        maximum_withdraw_amount=_provider_decimal(value.get("maxWithdrawAmount")),
        updated_at=_provider_time(value.get("updateTime")),
    )


def _position(value: dict[str, object]) -> BinanceUsdmPosition:
    return BinanceUsdmPosition(
        symbol=_text(value.get("symbol")),
        position_side=_text(value.get("positionSide")),
        amount=_provider_decimal(value.get("positionAmt")),
        entry_price=_provider_decimal(value.get("entryPrice")),
        mark_price=_provider_decimal(value.get("markPrice")),
        unrealized_pnl=_provider_decimal(value.get("unRealizedProfit")),
        isolated_margin=_provider_decimal(value.get("isolatedMargin")),
        notional=_provider_decimal(value.get("notional")),
        margin_asset=_text(value.get("marginAsset")),
        initial_margin=_provider_decimal(value.get("initialMargin")),
        maintenance_margin=_provider_decimal(value.get("maintMargin")),
        position_initial_margin=_provider_decimal(value.get("positionInitialMargin")),
        open_order_initial_margin=_provider_decimal(
            value.get("openOrderInitialMargin")
        ),
        updated_at=_provider_time(value.get("updateTime")),
    )


def _normal_order(value: dict[str, object]) -> BinanceUsdmOpenOrder:
    return BinanceUsdmOpenOrder(
        order_id=_integer(value.get("orderId")),
        client_order_id=_text(value.get("clientOrderId")),
        symbol=_text(value.get("symbol")),
        status=_text(value.get("status")),
        side=_side(value.get("side")),
        order_type=_text(value.get("type")),
        executed_quantity=_provider_decimal(value.get("executedQty")),
        original_quantity=_provider_decimal(value.get("origQty")),
        reduce_only=_boolean(value.get("reduceOnly")),
        close_position=_boolean(value.get("closePosition")),
    )


def _algo_order(value: dict[str, object]) -> BinanceUsdmOpenAlgoOrder:
    return BinanceUsdmOpenAlgoOrder(
        algo_id=_integer(value.get("algoId")),
        client_algo_id=_text(value.get("clientAlgoId")),
        symbol=_text(value.get("symbol")),
        status=_text(value.get("algoStatus")),
        side=_side(value.get("side")),
        order_type=_text(value.get("orderType")),
        quantity=_provider_decimal(value.get("quantity")),
        trigger_price=_provider_decimal(value.get("triggerPrice")),
        close_position=_boolean(value.get("closePosition")),
    )


def _trade(value: dict[str, object]) -> BinanceUsdmTradeFact:
    return BinanceUsdmTradeFact(
        trade_id=_integer(value.get("id")),
        order_id=_integer(value.get("orderId")),
        symbol=_text(value.get("symbol")),
        side=_side(value.get("side")),
        quantity=_provider_decimal(value.get("qty")),
        price=_provider_decimal(value.get("price")),
        commission=_provider_decimal(value.get("commission")),
        commission_asset=_text(value.get("commissionAsset")),
        realized_pnl=_provider_decimal(value.get("realizedPnl")),
        occurred_at=_provider_time(value.get("time")),
    )


def _income(value: dict[str, object]) -> BinanceUsdmIncomeFact:
    return BinanceUsdmIncomeFact(
        transaction_id=_integer(value.get("tranId")),
        trade_id=_text(value.get("tradeId"), blank=True),
        symbol=_text(value.get("symbol"), blank=True),
        income_type=_text(value.get("incomeType")),
        income=_provider_decimal(value.get("income")),
        asset=_text(value.get("asset")),
        occurred_at=_provider_time(value.get("time")),
    )


def _provider_decimal(value: object) -> Decimal:
    if type(value) is not str or not value:
        raise ValueError
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError from error
    if not parsed.is_finite():
        raise ValueError
    return parsed


def _exact_decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"{name} must be an exact finite Decimal")
    return value


def _provider_time(value: object) -> datetime:
    return _EPOCH + timedelta(milliseconds=_integer(value))


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError
    return value


def _text(value: object, *, blank: bool = False) -> str:
    if (
        type(value) is not str
        or (not blank and not value)
        or value.strip() != value
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError
    return value


def _side(value: object) -> str:
    side = _text(value)
    if side not in {"BUY", "SELL"}:
        raise ValueError
    return side


def _asset(value: str) -> None:
    if not value or not value.isascii() or not value.isalnum():
        raise ValueError("asset is invalid")


def _symbol(value: str) -> None:
    if not value or not value.isascii() or not value.isalnum():
        raise ValueError("symbol is invalid")


def _unique(values: Iterable[object], name: str) -> None:
    items = tuple(values)
    if len(items) != len(set(items)):
        raise ValueError(f"duplicate {name}")


def _merge_exact[ValueT](
    current: tuple[ValueT, ...],
    history: tuple[ValueT, ...],
    *,
    identity: Callable[[ValueT], object],
) -> tuple[ValueT, ...]:
    result: dict[object, ValueT] = {}
    for value in (*current, *history):
        key = identity(value)
        existing = result.get(key)
        if existing is not None and existing != value:
            raise ValueError("Binance USD-M order projections conflict")
        result[key] = value
    return tuple(result.values())


def _truncate_to_milliseconds(value: datetime) -> datetime:
    """The capture's own instant, at the resolution the venue speaks in.

    Binance takes `startTime` and `endTime` as whole milliseconds, and a real
    clock does not produce those. Demanding them of the caller made this
    function reachable only from a hand-written constant, which is why it had
    never been run against the live venue.

    Truncating rather than rounding, so the window never claims to have read
    an instant that had not arrived, and the truncated value is what the
    snapshot reports: the snapshot then says exactly which instant it queried
    instead of one a fraction of a millisecond later.
    """
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


def _epoch_ms(value: datetime) -> int:
    delta = value - _EPOCH
    milliseconds = delta // timedelta(milliseconds=1)
    if delta != timedelta(milliseconds=milliseconds):
        raise ValueError("account capture time requires exact milliseconds")
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
    "BinanceUsdmAccountCaptureError",
    "BinanceUsdmAccountReader",
    "BinanceUsdmAccountSnapshot",
    "BinanceUsdmBalance",
    "BinanceUsdmIncomeFact",
    "BinanceUsdmOpenAlgoOrder",
    "BinanceUsdmOpenOrder",
    "BinanceUsdmPosition",
    "BinanceUsdmTradeFact",
    "capture_binance_usdm_account",
)
