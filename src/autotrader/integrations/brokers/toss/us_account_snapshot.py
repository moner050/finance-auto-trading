from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from autotrader.integrations.brokers.common import BrokerResponse
from autotrader.shared.decimal import parse_contract_decimal

_HOLDINGS_KEYS = {
    "totalPurchaseAmount",
    "marketValue",
    "profitLoss",
    "dailyProfitLoss",
    "items",
}
_HOLDING_KEYS = {
    "symbol",
    "name",
    "marketCountry",
    "currency",
    "quantity",
    "lastPrice",
    "averagePurchasePrice",
    "marketValue",
    "profitLoss",
    "dailyProfitLoss",
    "cost",
}


class TossUsSnapshotUnavailable(RuntimeError):
    """Raised when a complete account-scoped US snapshot cannot be proven."""


@dataclass(frozen=True, slots=True)
class TossUsSnapshotCapture:
    account_scope_digest: bytes | None = field(repr=False)
    buying_power_response: BrokerResponse = field(repr=False)
    holdings_response: BrokerResponse = field(repr=False)
    sellable_responses: Mapping[str, BrokerResponse] = field(repr=False)


@dataclass(frozen=True, slots=True)
class TossUsCashFact:
    state: str
    available_cash: Decimal
    settled_cash: Decimal | None
    source_field: str
    provider_as_of: datetime
    captured_at: datetime
    source_digest: bytes


@dataclass(frozen=True, slots=True)
class TossUsPositionFact:
    symbol: str
    total_quantity: Decimal
    sellable_quantity: Decimal
    average_price: Decimal
    market_value: Decimal
    provider_as_of: datetime
    captured_at: datetime
    source_digest: bytes


@dataclass(frozen=True, slots=True)
class TossUsAccountSnapshot:
    account_scope_digest: bytes = field(repr=False)
    cash_fact: TossUsCashFact
    positions: tuple[TossUsPositionFact, ...]
    holdings_page_count: int
    sellable_page_count: int
    source_digest: bytes


@dataclass(frozen=True, slots=True)
class _Holding:
    symbol: str
    total_quantity: Decimal
    average_price: Decimal
    market_value: Decimal


async def capture_toss_us_snapshot(
    as_of: datetime,
    *,
    capture: TossUsSnapshotCapture,
    captured_at: datetime,
) -> TossUsAccountSnapshot:
    try:
        provider_time = _utc(as_of, "as_of")
        capture_time = _utc(captured_at, "captured_at")
        if capture_time < provider_time or type(capture) is not TossUsSnapshotCapture:
            raise ValueError("invalid capture time")
        scope = _digest(capture.account_scope_digest, "account scope")
        cash, cash_digest = _decode_usd_cash(capture.buying_power_response)
        holdings, holdings_digest = _decode_us_holdings(capture.holdings_response)
        responses = dict(capture.sellable_responses)
        if set(responses) != {holding.symbol for holding in holdings}:
            raise ValueError("sellable response coverage is incomplete")

        positions: list[TossUsPositionFact] = []
        sellable_digests: list[bytes] = []
        for holding in holdings:
            sellable, sellable_digest = _decode_sellable(
                responses[holding.symbol],
                maximum=holding.total_quantity,
            )
            sellable_digests.append(sellable_digest)
            positions.append(
                TossUsPositionFact(
                    symbol=holding.symbol,
                    total_quantity=holding.total_quantity,
                    sellable_quantity=sellable,
                    average_price=holding.average_price,
                    market_value=holding.market_value,
                    provider_as_of=provider_time,
                    captured_at=capture_time,
                    source_digest=_combined_digest(
                        b"TOSS_US_POSITION_FACT_V1",
                        holdings_digest,
                        sellable_digest,
                        holding.symbol.encode("ascii"),
                    ),
                )
            )
        positions.sort(key=lambda item: item.symbol)
        return TossUsAccountSnapshot(
            account_scope_digest=scope,
            cash_fact=TossUsCashFact(
                state="AVAILABLE",
                available_cash=cash,
                settled_cash=None,
                source_field="cashBuyingPower",
                provider_as_of=provider_time,
                captured_at=capture_time,
                source_digest=cash_digest,
            ),
            positions=tuple(positions),
            holdings_page_count=1,
            sellable_page_count=len(sellable_digests),
            source_digest=_combined_digest(
                b"TOSS_US_ACCOUNT_SNAPSHOT_V1",
                scope,
                cash_digest,
                holdings_digest,
                *sellable_digests,
            ),
        )
    except TossUsSnapshotUnavailable:
        raise
    except Exception:
        raise TossUsSnapshotUnavailable(
            "Toss US account snapshot is incomplete"
        ) from None


def _decode_usd_cash(response: BrokerResponse) -> tuple[Decimal, bytes]:
    result, digest = _result(response)
    if set(result) != {"currency", "cashBuyingPower"} or result.get("currency") != (
        "USD"
    ):
        raise ValueError("USD buying power is unavailable")
    cash = _decimal(result.get("cashBuyingPower"), "cashBuyingPower")
    if cash < 0:
        raise ValueError("cash buying power is negative")
    return cash, digest


def _decode_us_holdings(response: BrokerResponse) -> tuple[tuple[_Holding, ...], bytes]:
    result, digest = _result(response)
    if not set(result) >= _HOLDINGS_KEYS or not _overview_is_valid(result):
        raise ValueError("holdings overview is invalid")
    raw_items = result.get("items")
    if type(raw_items) is not list:
        raise ValueError("holdings items are invalid")

    holdings: list[_Holding] = []
    identities: set[str] = set()
    for raw in cast(list[object], raw_items):
        item = _mapping(raw, "holding")
        if not set(item) >= _HOLDING_KEYS or not _holding_details_are_valid(item):
            raise ValueError("holding item is invalid")
        country = item.get("marketCountry")
        currency = item.get("currency")
        if country == "KR" and currency == "KRW":
            continue
        if country != "US" or currency != "USD":
            raise ValueError("holding scope is invalid")
        symbol = _symbol(item.get("symbol"))
        if symbol in identities:
            raise ValueError("duplicate US holding")
        identities.add(symbol)
        quantity = _decimal(item.get("quantity"), "quantity")
        average_price = _decimal(item.get("averagePurchasePrice"), "average price")
        market_value_record = _mapping(item.get("marketValue"), "market value")
        market_value = _decimal(market_value_record.get("amount"), "market value")
        if quantity <= 0 or average_price < 0 or market_value < 0:
            raise ValueError("holding value is invalid")
        holdings.append(
            _Holding(
                symbol=symbol,
                total_quantity=quantity,
                average_price=average_price,
                market_value=market_value,
            )
        )
    holdings.sort(key=lambda item: item.symbol)
    return tuple(holdings), digest


def _decode_sellable(
    response: BrokerResponse,
    *,
    maximum: Decimal,
) -> tuple[Decimal, bytes]:
    result, digest = _result(response)
    if set(result) != {"sellableQuantity"}:
        raise ValueError("sellable response is invalid")
    quantity = _decimal(result.get("sellableQuantity"), "sellable quantity")
    if quantity < 0 or quantity > maximum:
        raise ValueError("sellable quantity is invalid")
    return quantity, digest


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


def _overview_is_valid(result: Mapping[str, object]) -> bool:
    try:
        purchase = _mapping(result.get("totalPurchaseAmount"), "purchase")
        market = _mapping(result.get("marketValue"), "market")
        profit = _mapping(result.get("profitLoss"), "profit")
        daily = _mapping(result.get("dailyProfitLoss"), "daily")
        if (
            not _price_is_valid(purchase)
            or not set(market) >= {"amount", "amountAfterCost"}
            or not _price_is_valid(_mapping(market["amount"], "market amount"))
            or not _price_is_valid(_mapping(market["amountAfterCost"], "market cost"))
            or not set(profit) >= {"amount", "amountAfterCost", "rate", "rateAfterCost"}
            or not _price_is_valid(_mapping(profit["amount"], "profit amount"))
            or not _price_is_valid(_mapping(profit["amountAfterCost"], "profit cost"))
            or not set(daily) >= {"amount", "rate"}
            or not _price_is_valid(_mapping(daily["amount"], "daily amount"))
        ):
            return False
        _decimal(profit["rate"], "profit rate")
        _decimal(profit["rateAfterCost"], "profit cost rate")
        _decimal(daily["rate"], "daily rate")
        return True
    except TypeError, ValueError, ArithmeticError:
        return False


def _holding_details_are_valid(item: Mapping[str, object]) -> bool:
    try:
        name = item.get("name")
        _symbol(item.get("symbol"))
        _decimal(item.get("quantity"), "quantity")
        _decimal(item.get("lastPrice"), "last price")
        _decimal(item.get("averagePurchasePrice"), "average price")
        market = _mapping(item.get("marketValue"), "market value")
        profit = _mapping(item.get("profitLoss"), "profit loss")
        daily = _mapping(item.get("dailyProfitLoss"), "daily profit loss")
        cost = _mapping(item.get("cost"), "cost")
        if (
            type(name) is not str
            or not name
            or "\n" in name
            or not set(market) >= {"purchaseAmount", "amount", "amountAfterCost"}
            or not set(profit) >= {"amount", "amountAfterCost", "rate", "rateAfterCost"}
            or not set(daily) >= {"amount", "rate"}
            or "commission" not in cost
        ):
            return False
        for key in ("purchaseAmount", "amount", "amountAfterCost"):
            _decimal(market[key], key)
        for key in ("amount", "amountAfterCost", "rate", "rateAfterCost"):
            _decimal(profit[key], key)
        for key in ("amount", "rate"):
            _decimal(daily[key], key)
        _nullable_decimal(cost.get("commission"), "commission")
        _nullable_decimal(cost.get("tax"), "tax")
        return True
    except TypeError, ValueError, ArithmeticError:
        return False


def _price_is_valid(value: Mapping[str, object]) -> bool:
    try:
        if "krw" not in value:
            return False
        _decimal(value["krw"], "KRW price")
        if value.get("usd") is not None:
            _decimal(value["usd"], "USD price")
        return True
    except TypeError, ValueError, ArithmeticError:
        return False


def _nullable_decimal(value: object, name: str) -> Decimal | None:
    return None if value is None else _decimal(value, name)


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is not str:
        raise TypeError(f"{name} must be decimal text")
    return parse_contract_decimal(value)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    raw = cast(Mapping[object, object], value)
    if any(type(key) is not str for key in raw):
        raise TypeError(f"{name} must have string keys")
    return cast(Mapping[str, object], raw)


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


def _utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _digest(value: object, name: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        raise TypeError(f"{name} must be SHA-256 bytes")
    return value


def _combined_digest(tag: bytes, *values: bytes) -> bytes:
    digest = hashlib.sha256()
    for value in (tag, *values):
        digest.update(len(value).to_bytes(4, "big"))
        digest.update(value)
    return digest.digest()
