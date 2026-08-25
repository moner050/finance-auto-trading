from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol, cast

from autotrader.shared.decimal import decimal_to_string, require_decimal

_KRX_STOCK = "KRX_STOCK"
_US_STOCK = "US_STOCK"
_KRW_HIGH_VALUE_ORDER_THRESHOLD = Decimal("100000000")


class _StockOrderCommand(Protocol):
    @property
    def command_type(self) -> object: ...

    @property
    def target_broker_order_id(self) -> object: ...

    @property
    def broker_client_order_id(self) -> object: ...

    @property
    def not_after(self) -> datetime: ...

    @property
    def side(self) -> object: ...

    @property
    def order_style(self) -> object: ...

    @property
    def quantity(self) -> object: ...

    @property
    def limit_price(self) -> object: ...

    @property
    def time_in_force(self) -> object: ...


class _OrderAcknowledgementResponse(Protocol):
    @property
    def status(self) -> int: ...

    @property
    def body(self) -> bytes: ...


class TossStockOrderPreviewError(ValueError):
    """Safe public failure for Toss stock-order preview data."""


@dataclass(frozen=True, slots=True)
class TossStockOrderPreview:
    """A non-transmitting Toss stock order payload with its account scope."""

    account_seq: str
    client_order_id: str
    body: bytes

    def __post_init__(self) -> None:
        _account_sequence(int(self.account_seq))
        _toss_client_order_id(self.client_order_id)
        if not self.body:
            raise ValueError("Toss stock order preview is invalid")


@dataclass(frozen=True, slots=True)
class TossOrderSubmissionAcknowledgement:
    """A provider order identifier correlated to the submitted client identifier."""

    order_id: str
    client_order_id: str

    def __post_init__(self) -> None:
        if not self.order_id or "\n" in self.order_id:
            raise ValueError("Toss acknowledged order id is invalid")
        _toss_client_order_id(self.client_order_id)


def build_toss_stock_order_preview(
    *,
    command: _StockOrderCommand,
    account_seq: object,
    market: object,
    symbol: str,
    now: datetime,
) -> TossStockOrderPreview:
    """Build the documented Toss stock request body without network access."""
    try:
        return _build_toss_stock_order_preview(
            command=command,
            account_seq=account_seq,
            market=market,
            symbol=symbol,
            now=now,
        )
    except Exception as caught:
        _scrub_toss_order_preview_error(caught)
        del caught, command, account_seq, market, symbol, now
        raise TossStockOrderPreviewError(
            "Toss stock order preview is unavailable"
        ) from None


def _build_toss_stock_order_preview(
    *,
    command: _StockOrderCommand,
    account_seq: object,
    market: object,
    symbol: str,
    now: datetime,
) -> TossStockOrderPreview:
    if (
        _enum_value(
            command.command_type,
            module="autotrader.execution.orders.models",
            name="CommandType",
        )
        != "SUBMIT"
    ):
        raise ValueError("Toss stock preview requires a submit command")
    if command.target_broker_order_id is not None:
        raise ValueError("Toss stock submit command cannot target an existing order")
    if not _is_utc(now):
        raise ValueError("Toss stock preview requires UTC now")
    if not _is_utc(command.not_after) or now >= command.not_after:
        raise ValueError("Toss stock preview command not_after is expired")
    market_name = _enum_value(
        market,
        module="autotrader.integrations.brokers.common",
        name="BrokerMarket",
    )
    if market_name not in {_KRX_STOCK, _US_STOCK}:
        raise ValueError("Toss does not support the requested market")
    normalized_symbol = _stock_symbol(symbol)
    normalized_account = _account_sequence(account_seq)
    client_order_id = _toss_client_order_id(command.broker_client_order_id)
    quantity = require_decimal(command.quantity)
    if quantity <= 0:
        raise ValueError("Toss stock preview requires a positive quantity")
    order_style = _enum_value(
        command.order_style,
        module="autotrader.domain.enums",
        name="OrderStyle",
    )
    side = _enum_value(
        command.side,
        module="autotrader.domain.enums",
        name="Side",
    )
    if side not in {"BUY", "SELL"}:
        raise ValueError("Toss stock preview side is unsupported")
    _toss_quantity_allows(
        quantity=quantity,
        market=market_name,
        order_style=order_style,
        side=side,
    )
    order_type, price, numeric_price = _toss_order_terms(
        order_style=order_style,
        limit_price=command.limit_price,
    )
    _reject_unconfirmed_krw_high_value_order(
        market=market_name,
        quantity=quantity,
        price=numeric_price,
    )
    time_in_force = _toss_time_in_force(
        command.time_in_force,
        market=market_name,
        order_style=order_style,
    )
    body: dict[str, str | bool] = {
        "clientOrderId": client_order_id,
        "confirmHighValueOrder": False,
        "orderType": order_type,
        "quantity": decimal_to_string(quantity),
        "side": side,
        "symbol": normalized_symbol,
        "timeInForce": time_in_force,
    }
    if price is not None:
        body["price"] = price
    return TossStockOrderPreview(
        account_seq=normalized_account,
        client_order_id=client_order_id,
        body=json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )


def decode_toss_order_submission_acknowledgement(
    response: _OrderAcknowledgementResponse, *, preview: TossStockOrderPreview
) -> TossOrderSubmissionAcknowledgement:
    """Validate the documented Toss order acknowledgement correlation fields."""
    try:
        return _decode_toss_order_submission_acknowledgement(
            response=response, preview=preview
        )
    except Exception as caught:
        _scrub_toss_order_preview_error(caught)
        del caught, response, preview
        raise TossStockOrderPreviewError(
            "Toss stock order acknowledgement is unavailable"
        ) from None


def _decode_toss_order_submission_acknowledgement(
    *,
    response: _OrderAcknowledgementResponse,
    preview: TossStockOrderPreview,
) -> TossOrderSubmissionAcknowledgement:
    if response.status != 200:
        raise ValueError("Toss order acknowledgement is not successful")
    try:
        payload: object = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Toss order acknowledgement is not valid JSON") from error
    if not isinstance(payload, Mapping):
        raise ValueError("Toss order acknowledgement is not an object")
    payload = cast(Mapping[str, object], payload)
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("Toss order acknowledgement result is invalid")
    result = cast(Mapping[str, object], result)
    order_id = result.get("orderId")
    client_order_id = result.get("clientOrderId")
    if not isinstance(order_id, str) or not isinstance(client_order_id, str):
        raise ValueError("Toss order acknowledgement is incomplete")
    if client_order_id != preview.client_order_id:
        raise ValueError("Toss order acknowledgement client order id does not match")
    return TossOrderSubmissionAcknowledgement(
        order_id=order_id,
        client_order_id=client_order_id,
    )


def _enum_value(value: object, *, module: str, name: str) -> str | None:
    canonical_module = sys.modules.get(module)
    canonical_type = (
        None if canonical_module is None else getattr(canonical_module, name, None)
    )
    if not isinstance(canonical_type, type) or type(value) is not canonical_type:
        return None
    enum_value = getattr(value, "value", None)
    if type(enum_value) is str:
        return enum_value
    return None


def _is_utc(value: object) -> bool:
    return (
        type(value) is datetime
        and value.tzinfo is UTC
        and value.utcoffset() == UTC.utcoffset(value)
    )


def _account_sequence(value: object) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("account sequence must be a positive integer")
    return str(value)


def _toss_client_order_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 36
        or not value.isascii()
        or any(not (character.isalnum() or character in "-_") for character in value)
    ):
        raise ValueError(
            "Toss client order id must be 1 through 36 URL-safe characters"
        )
    return value


def _toss_quantity_allows(
    *,
    quantity: Decimal,
    market: str,
    order_style: str | None,
    side: str,
) -> None:
    if quantity == quantity.to_integral_value():
        return
    if (
        market != _US_STOCK
        or order_style != "MARKET"
        or side != "SELL"
        or -cast(int, quantity.as_tuple().exponent) > 6
    ):
        raise ValueError("Toss stock preview requires a whole quantity")


def _toss_order_terms(
    *, order_style: str | None, limit_price: object
) -> tuple[str, str | None, Decimal | None]:
    if order_style == "MARKET":
        if limit_price is not None:
            raise ValueError("Toss market preview cannot carry a limit price")
        return "MARKET", None, None
    if order_style != "LIMIT":
        raise ValueError("Toss stock preview supports market or limit orders")
    if limit_price is None:
        raise ValueError("Toss limit preview requires a limit price")
    price = require_decimal(limit_price)
    if price <= 0:
        raise ValueError("Toss limit preview requires a positive limit price")
    return "LIMIT", decimal_to_string(price), price


def _reject_unconfirmed_krw_high_value_order(
    *, market: str, quantity: Decimal, price: Decimal | None
) -> None:
    if (
        market == _KRX_STOCK
        and price is not None
        and quantity * price >= _KRW_HIGH_VALUE_ORDER_THRESHOLD
    ):
        raise ValueError("Toss high-value KRX order requires explicit confirmation")


def _toss_time_in_force(value: object, *, market: str, order_style: str | None) -> str:
    if value == "DAY":
        return "DAY"
    if value == "CLS" and market == _US_STOCK and order_style == "LIMIT":
        return "CLS"
    raise ValueError("Toss stock preview time in force is unsupported")


def _stock_symbol(value: str) -> str:
    symbol = value.upper()
    if symbol in {"NQ", "MNQ"}:
        raise ValueError("Toss does not support the requested futures root")
    if not symbol or any(
        not (character.isalnum() or character in ".-") for character in symbol
    ):
        raise ValueError("stock symbol is invalid")
    return symbol


def _scrub_toss_order_preview_error(caught: Exception) -> None:
    caught.__traceback__ = None
    caught.__context__ = None
    caught.__cause__ = None
    caught.args = ()
    caught.__dict__.clear()
