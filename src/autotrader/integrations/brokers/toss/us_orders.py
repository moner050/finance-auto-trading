from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

from autotrader.integrations.brokers.common import BrokerResponse
from autotrader.shared.decimal import parse_contract_decimal

_ORDER_REQUIRED = {
    "orderId",
    "symbol",
    "side",
    "orderType",
    "timeInForce",
    "status",
    "quantity",
    "currency",
    "orderedAt",
    "execution",
}
_EXECUTION_REQUIRED = {
    "filledQuantity",
    "averageFilledPrice",
    "filledAmount",
    "commission",
    "tax",
    "filledAt",
    "settlementDate",
}
_ORDER_STATES = {
    "PENDING",
    "PENDING_CANCEL",
    "PENDING_REPLACE",
    "PARTIAL_FILLED",
    "FILLED",
    "CANCELED",
    "REJECTED",
    "CANCEL_REJECTED",
    "REPLACE_REJECTED",
    "REPLACED",
}


class TossUsOrdersUnavailable(RuntimeError):
    """Raised when complete account-scoped order history cannot be proven."""


@dataclass(frozen=True, slots=True)
class ProviderTimeWindow:
    started_at: datetime
    ended_at: datetime

    def __post_init__(self) -> None:
        started = _utc(self.started_at, "started_at")
        ended = _utc(self.ended_at, "ended_at")
        if started >= ended:
            raise ValueError("provider time window must be positive")
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "ended_at", ended)


@dataclass(frozen=True, slots=True)
class TossUsOrderPage:
    requested_cursor: str | None
    response: BrokerResponse = field(repr=False)


@dataclass(frozen=True, slots=True)
class TossUsOrderCapture:
    status: str
    account_scope_digest: bytes | None = field(repr=False)
    pages: tuple[TossUsOrderPage, ...]


@dataclass(frozen=True, slots=True)
class TossUsOrderFact:
    provider_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    cumulative_fill_quantity: Decimal
    state: str
    limit_price: Decimal | None
    commission: Decimal | None
    tax: Decimal | None
    settlement_asset: str
    ordered_at: datetime
    filled_at: datetime | None
    canceled_at: datetime | None
    provider_as_of: datetime
    captured_at: datetime
    source_digest: bytes


async def read_toss_us_orders(
    window: ProviderTimeWindow,
    *,
    captures: tuple[TossUsOrderCapture, ...],
    captured_at: datetime,
) -> tuple[TossUsOrderFact, ...]:
    try:
        if type(window) is not ProviderTimeWindow:
            raise TypeError("window is invalid")
        capture_time = _utc(captured_at, "captured_at")
        if capture_time < window.ended_at:
            raise ValueError("capture precedes the requested window")
        if type(captures) is not tuple or len(captures) != 2:
            raise ValueError("OPEN and CLOSED captures are required")
        by_status: dict[str, TossUsOrderCapture] = {}
        scope: bytes | None = None
        for capture in captures:
            if type(capture) is not TossUsOrderCapture:
                raise TypeError("order capture is invalid")
            if capture.status not in {"OPEN", "CLOSED"} or capture.status in by_status:
                raise ValueError("order capture status is invalid")
            capture_scope = _digest(capture.account_scope_digest, "account scope")
            if scope is None:
                scope = capture_scope
            elif scope != capture_scope:
                raise ValueError("order capture scope changed")
            by_status[capture.status] = capture
        if set(by_status) != {"OPEN", "CLOSED"}:
            raise ValueError("OPEN and CLOSED captures are required")

        facts: list[TossUsOrderFact] = []
        identities: set[str] = set()
        for status in ("OPEN", "CLOSED"):
            capture = by_status[status]
            for raw_order, source_digest in _decode_pages(capture):
                order_id = _opaque_id(raw_order.get("orderId"))
                if order_id in identities:
                    raise ValueError("duplicate provider order identity")
                identities.add(order_id)
                fact = _decode_order(
                    raw_order,
                    order_id=order_id,
                    window=window,
                    captured_at=capture_time,
                    source_digest=source_digest,
                )
                if fact is not None:
                    facts.append(fact)
        facts.sort(key=lambda item: item.provider_order_id)
        return tuple(facts)
    except TossUsOrdersUnavailable:
        raise
    except Exception:
        raise TossUsOrdersUnavailable("Toss US orders evidence is incomplete") from None


def _decode_pages(
    capture: TossUsOrderCapture,
) -> tuple[tuple[Mapping[str, object], bytes], ...]:
    if type(capture.pages) is not tuple or not capture.pages:
        raise ValueError("order pages are missing")
    if capture.status == "OPEN" and len(capture.pages) != 1:
        raise ValueError("OPEN orders must be unpaginated")
    result: list[tuple[Mapping[str, object], bytes]] = []
    expected_cursor: str | None = None
    for ordinal, page in enumerate(capture.pages):
        if (
            type(page) is not TossUsOrderPage
            or page.requested_cursor != expected_cursor
        ):
            raise ValueError("order cursor chain is invalid")
        payload, digest = _result(page.response)
        if set(payload) != {"orders", "nextCursor", "hasNext"}:
            raise ValueError("order page shape is invalid")
        raw_orders = payload.get("orders")
        has_next = payload.get("hasNext")
        next_cursor = payload.get("nextCursor")
        if type(raw_orders) is not list or type(has_next) is not bool:
            raise ValueError("order page values are invalid")
        if has_next:
            if (
                capture.status != "CLOSED"
                or type(next_cursor) is not str
                or not next_cursor
                or "\n" in next_cursor
                or ordinal == len(capture.pages) - 1
            ):
                raise ValueError("order page is incomplete")
            expected_cursor = next_cursor
        else:
            if next_cursor is not None or ordinal != len(capture.pages) - 1:
                raise ValueError("order page chain ended early")
            expected_cursor = None
        for raw_order in cast(list[object], raw_orders):
            result.append((_mapping(raw_order, "order"), digest))
    return tuple(result)


def _decode_order(
    order: Mapping[str, object],
    *,
    order_id: str,
    window: ProviderTimeWindow,
    captured_at: datetime,
    source_digest: bytes,
) -> TossUsOrderFact | None:
    if not set(order) >= _ORDER_REQUIRED:
        raise ValueError("order shape is invalid")
    symbol = _symbol(order.get("symbol"))
    side = order.get("side")
    order_type = order.get("orderType")
    time_in_force = order.get("timeInForce")
    state = order.get("status")
    currency = order.get("currency")
    if (
        side not in {"BUY", "SELL"}
        or order_type not in {"LIMIT", "MARKET"}
        or time_in_force not in {"DAY", "CLS", "OPG"}
        or state not in _ORDER_STATES
        or currency not in {"KRW", "USD"}
    ):
        raise ValueError("order scope is invalid")
    quantity = _decimal(order.get("quantity"), "quantity")
    price = _nullable_decimal(order.get("price"), "price")
    if quantity <= 0 or (order_type == "LIMIT" and (price is None or price <= 0)):
        raise ValueError("order values are invalid")
    if order_type == "MARKET" and price is not None:
        raise ValueError("market order price must be null")

    ordered_at = _timestamp(order.get("orderedAt"), "orderedAt")
    canceled_at = _nullable_timestamp(order.get("canceledAt"), "canceledAt")
    if not window.started_at <= ordered_at < window.ended_at:
        raise ValueError("order is outside the provider window")
    if canceled_at is not None and (
        canceled_at < ordered_at or canceled_at > captured_at
    ):
        raise ValueError("canceledAt is invalid")

    execution = _mapping(order.get("execution"), "execution")
    if not set(execution) >= _EXECUTION_REQUIRED:
        raise ValueError("execution shape is invalid")
    filled = _decimal(execution.get("filledQuantity"), "filledQuantity")
    average = _nullable_decimal(
        execution.get("averageFilledPrice"),
        "averageFilledPrice",
    )
    filled_amount = _nullable_decimal(execution.get("filledAmount"), "filledAmount")
    commission = _nullable_decimal(execution.get("commission"), "commission")
    tax = _nullable_decimal(execution.get("tax"), "tax")
    filled_at = _nullable_timestamp(execution.get("filledAt"), "filledAt")
    settlement_date = execution.get("settlementDate")
    _optional_date(settlement_date)
    if (
        filled < 0
        or filled > quantity
        or (average is not None and average < 0)
        or (filled_amount is not None and filled_amount < 0)
        or (commission is not None and commission < 0)
        or (tax is not None and tax < 0)
    ):
        raise ValueError("execution values are invalid")
    if filled > 0 and (average is None or filled_amount is None or filled_at is None):
        raise ValueError("filled execution is incomplete")
    if filled_at is not None and (filled_at < ordered_at or filled_at > captured_at):
        raise ValueError("filledAt is invalid")

    if currency == "KRW":
        return None
    return TossUsOrderFact(
        provider_order_id=order_id,
        symbol=symbol,
        side=cast(str, side),
        quantity=quantity,
        cumulative_fill_quantity=filled,
        state=cast(str, state),
        limit_price=price,
        commission=commission,
        tax=tax,
        settlement_asset="USD",
        ordered_at=ordered_at,
        filled_at=filled_at,
        canceled_at=canceled_at,
        provider_as_of=captured_at,
        captured_at=captured_at,
        source_digest=source_digest,
    )


def _result(response: BrokerResponse) -> tuple[Mapping[str, object], bytes]:
    if type(response) is not BrokerResponse or response.status != 200:
        raise ValueError("provider response is invalid")
    try:
        payload: object = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("provider response is invalid") from error
    envelope = _mapping(payload, "provider envelope")
    if set(envelope) != {"result"}:
        raise ValueError("provider envelope is invalid")
    return _mapping(envelope["result"], "provider result"), hashlib.sha256(
        response.body
    ).digest()


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    raw = cast(Mapping[object, object], value)
    if any(type(key) is not str for key in raw):
        raise TypeError(f"{name} must have string keys")
    return cast(Mapping[str, object], raw)


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is not str:
        raise TypeError(f"{name} must be decimal text")
    return parse_contract_decimal(value)


def _nullable_decimal(value: object, name: str) -> Decimal | None:
    return None if value is None else _decimal(value, name)


def _timestamp(value: object, name: str) -> datetime:
    if type(value) is not str or not value or "\n" in value:
        raise TypeError(f"{name} must be ISO-8601 text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} is invalid") from error
    return _utc(parsed, name)


def _nullable_timestamp(value: object, name: str) -> datetime | None:
    return None if value is None else _timestamp(value, name)


def _optional_date(value: object) -> None:
    if value is None:
        return
    if type(value) is not str:
        raise TypeError("settlementDate must be ISO date text")
    date.fromisoformat(value)


def _utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _digest(value: object, name: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        raise TypeError(f"{name} must be SHA-256 bytes")
    return value


def _opaque_id(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 128
        or not value.isascii()
        or "\n" in value
    ):
        raise ValueError("provider order identity is invalid")
    return value


def _symbol(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 32
        or not value.isascii()
        or any(not (character.isalnum() or character in ".-") for character in value)
    ):
        raise ValueError("symbol is invalid")
    return value
