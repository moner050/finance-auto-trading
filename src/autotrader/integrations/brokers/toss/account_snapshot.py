from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol, cast

from autotrader.integrations.brokers.common import BrokerResponse
from autotrader.shared.decimal import (
    decimal_to_string,
    parse_contract_decimal,
    require_decimal,
)

_HOLDINGS_OVERVIEW_KEYS = frozenset(
    {
        "totalPurchaseAmount",
        "marketValue",
        "profitLoss",
        "dailyProfitLoss",
        "items",
    }
)
_HOLDINGS_ITEM_KEYS = frozenset(
    {
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
)


@dataclass(frozen=True, slots=True)
class TossHoldingPosition:
    symbol: str
    market_country: str
    currency: str
    total_quantity: Decimal

    def __post_init__(self) -> None:
        quantity = require_decimal(self.total_quantity)
        if self.market_country == "KR":
            if (
                self.currency != "KRW"
                or not _is_kr_domestic_symbol(self.symbol)
                or quantity <= 0
                or quantity != quantity.to_integral_value()
            ):
                raise ValueError("Toss holding position is invalid")
        elif self.market_country == "US":
            if (
                self.currency != "USD"
                or not _is_us_symbol(self.symbol)
                or quantity <= 0
            ):
                raise ValueError("Toss holding position is invalid")
        else:
            raise ValueError("Toss holding position is invalid")
        object.__setattr__(self, "total_quantity", quantity)


@dataclass(frozen=True, slots=True)
class TossKrDomesticSellablePosition:
    symbol: str
    total_quantity: Decimal
    sellable_quantity: Decimal

    def __post_init__(self) -> None:
        total = require_decimal(self.total_quantity)
        sellable = require_decimal(self.sellable_quantity)
        if (
            not _is_kr_domestic_symbol(self.symbol)
            or total <= 0
            or total != total.to_integral_value()
            or sellable < 0
            or sellable != sellable.to_integral_value()
            or sellable > total
        ):
            raise ValueError("Toss KR domestic sellable position is invalid")
        object.__setattr__(self, "total_quantity", total)
        object.__setattr__(self, "sellable_quantity", sellable)


@dataclass(frozen=True, slots=True)
class TossStableKrDomesticCashAccountSnapshot:
    observed_at: datetime
    cash_buying_power: Decimal
    positions: tuple[TossKrDomesticSellablePosition, ...]
    source_hash: bytes

    def __post_init__(self) -> None:
        if (
            type(self.observed_at) is not datetime
            or self.observed_at.tzinfo is not UTC
            or self.observed_at.microsecond != 0
        ):
            raise ValueError("Toss account snapshot observed time is invalid")
        cash = require_decimal(self.cash_buying_power)
        raw_positions = cast(object, self.positions)
        raw_hash = cast(object, self.source_hash)
        if (
            cash < 0
            or cash != cash.to_integral_value()
            or not isinstance(raw_positions, tuple)
            or not all(
                type(position) is TossKrDomesticSellablePosition
                for position in cast(tuple[object, ...], raw_positions)
            )
        ):
            raise ValueError("Toss stable KR domestic cash snapshot is invalid")
        positions = cast(tuple[TossKrDomesticSellablePosition, ...], raw_positions)
        symbols = tuple(position.symbol for position in positions)
        if (
            symbols != tuple(sorted(symbols))
            or len(set(symbols)) != len(symbols)
            or type(raw_hash) is not bytes
            or len(raw_hash) != 32
            or raw_hash != _snapshot_hash(cash, positions)
        ):
            raise ValueError("Toss stable KR domestic cash snapshot is invalid")
        object.__setattr__(self, "cash_buying_power", cash)
        object.__setattr__(self, "positions", positions)


@dataclass(frozen=True, slots=True)
class _TossKrDomesticCashCapture:
    cash_buying_power: Decimal
    positions: tuple[TossKrDomesticSellablePosition, ...]


class _TossAccountSnapshotReadPort(Protocol):
    def read_holdings(
        self, *, access_token: str, account_seq: int
    ) -> Coroutine[object, object, BrokerResponse]: ...

    def read_krw_cash_buying_power(
        self, *, access_token: str, account: object
    ) -> Coroutine[object, object, BrokerResponse]: ...

    def read_sellable_quantity(
        self, *, access_token: str, account_seq: int, symbol: str
    ) -> Coroutine[object, object, BrokerResponse]: ...


async def collect_stable_toss_kr_domestic_cash_account_snapshot(
    *,
    adapter: object,
    access_token: object,
    account: object,
    max_sellable_reads: object,
    clock: Callable[[], datetime] | None = None,
) -> TossStableKrDomesticCashAccountSnapshot:
    first: object = None
    second: object = None
    observed_at: object = None
    snapshot: TossStableKrDomesticCashAccountSnapshot | None = None
    incomplete = False
    try:
        from autotrader.integrations.brokers.toss.adapter import TossAccount

        if (
            type(access_token) is not str
            or not access_token
            or "\n" in access_token
            or type(account) is not TossAccount
            or account.account_type != "BROKERAGE"
            or type(max_sellable_reads) is not int
            or max_sellable_reads <= 0
        ):
            raise ValueError("Toss account snapshot input is invalid")
        account.__post_init__()
        observed_at = (_utc_now if clock is None else clock)()
        if (
            type(observed_at) is not datetime
            or observed_at.tzinfo is not UTC
            or observed_at.microsecond != 0
        ):
            raise ValueError("Toss account snapshot observed time is invalid")
        first = await _capture_account_facts(
            adapter=adapter,
            access_token=access_token,
            account=account,
            max_sellable_reads=max_sellable_reads,
        )
        second = await _capture_account_facts(
            adapter=adapter,
            access_token=access_token,
            account=account,
            max_sellable_reads=max_sellable_reads,
        )
        if first != second:
            raise ValueError("Toss account snapshot changed during collection")
        capture = first
        snapshot = TossStableKrDomesticCashAccountSnapshot(
            observed_at=observed_at,
            cash_buying_power=capture.cash_buying_power,
            positions=capture.positions,
            source_hash=_snapshot_hash(
                capture.cash_buying_power,
                capture.positions,
            ),
        )
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as caught:
        _scrub_control(caught)
        del caught
        raise
    except Exception as caught:
        _scrub_exception(caught)
        del caught
        incomplete = True
    finally:
        first = None
        second = None
        observed_at = None
        del adapter, access_token, account, clock
    if incomplete or snapshot is None:
        raise _incomplete_account_snapshot_error()
    return snapshot


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _incomplete_account_snapshot_error() -> RuntimeError:
    from autotrader.integrations.brokers.toss.adapter import (
        TossIncompleteAccountSnapshot,
    )

    return TossIncompleteAccountSnapshot("Toss account snapshot is incomplete")


async def _capture_account_facts(
    *,
    adapter: object,
    access_token: str,
    account: object,
    max_sellable_reads: int,
) -> _TossKrDomesticCashCapture:
    from autotrader.integrations.brokers.toss.adapter import (
        TossAccount,
        decode_toss_krw_cash_buying_power,
    )

    holdings_response: object = None
    buying_power_response: object = None
    sellable_response: object = None
    holdings: object = None
    positions: list[TossKrDomesticSellablePosition] = []
    try:
        reader = cast(_TossAccountSnapshotReadPort, adapter)
        exact_account = cast(TossAccount, account)
        holdings_response = await reader.read_holdings(
            access_token=access_token,
            account_seq=exact_account.account_seq,
        )
        holdings = decode_toss_holdings(holdings_response)
        domestic_holdings = tuple(
            sorted(
                (holding for holding in holdings if holding.market_country == "KR"),
                key=lambda holding: holding.symbol,
            )
        )
        if len(domestic_holdings) > max_sellable_reads:
            raise ValueError("Toss sellable read budget is insufficient")
        buying_power_response = await reader.read_krw_cash_buying_power(
            access_token=access_token,
            account=exact_account,
        )
        buying_power = decode_toss_krw_cash_buying_power(buying_power_response)
        for holding in domestic_holdings:
            sellable_response = await reader.read_sellable_quantity(
                access_token=access_token,
                account_seq=exact_account.account_seq,
                symbol=holding.symbol,
            )
            positions.append(
                TossKrDomesticSellablePosition(
                    symbol=holding.symbol,
                    total_quantity=holding.total_quantity,
                    sellable_quantity=decode_toss_sellable_quantity(sellable_response),
                )
            )
            sellable_response = None
        return _TossKrDomesticCashCapture(
            cash_buying_power=buying_power.amount,
            positions=tuple(positions),
        )
    finally:
        holdings_response = None
        buying_power_response = None
        sellable_response = None
        holdings = None
        positions.clear()
        del adapter, access_token, account


def _snapshot_hash(
    cash_buying_power: Decimal,
    positions: tuple[TossKrDomesticSellablePosition, ...],
) -> bytes:
    payload = {
        "cashBuyingPower": _integer_text(cash_buying_power),
        "positions": [
            {
                "sellableQuantity": _integer_text(position.sellable_quantity),
                "symbol": position.symbol,
                "totalQuantity": _integer_text(position.total_quantity),
            }
            for position in positions
        ],
        "provider": "TOSS",
        "scope": "KR_DOMESTIC_SIX_DIGIT_HOLDINGS",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).digest()


def _integer_text(value: Decimal) -> str:
    integral = value.to_integral_value()
    return decimal_to_string(Decimal() if integral == 0 else integral)


def _scrub_control(
    caught: asyncio.CancelledError | KeyboardInterrupt | SystemExit,
) -> None:
    caught.__traceback__ = None
    caught.__context__ = None
    caught.__cause__ = None
    caught.args = ()
    caught.__dict__.clear()
    if isinstance(caught, SystemExit):
        caught.code = 1


def _scrub_exception(caught: Exception) -> None:
    caught.__traceback__ = None
    caught.__context__ = None
    caught.__cause__ = None
    caught.args = ()
    caught.__dict__.clear()


def decode_toss_holdings(response: BrokerResponse) -> tuple[TossHoldingPosition, ...]:
    status = response.status
    body = response.body
    del response
    try:
        positions = _decode_holdings(status, body)
    finally:
        del body
    if positions is None:
        raise ValueError("Toss holdings response is invalid") from None
    return positions


def decode_toss_sellable_quantity(response: BrokerResponse) -> Decimal:
    status = response.status
    body = response.body
    del response
    try:
        quantity = _decode_sellable_quantity(status, body)
    finally:
        del body
    if quantity is None:
        raise ValueError("Toss sellable quantity response is invalid") from None
    return quantity


def _decode_holdings(
    status: int, body: bytes
) -> tuple[TossHoldingPosition, ...] | None:
    if status != 200:
        return None
    try:
        payload: object = json.loads(body)
    except UnicodeDecodeError, json.JSONDecodeError, RecursionError:
        return None
    if not isinstance(payload, Mapping):
        return None
    result = cast(Mapping[str, object], payload).get("result")
    if not isinstance(result, Mapping):
        return None
    result = cast(Mapping[str, object], result)
    if not result.keys() >= _HOLDINGS_OVERVIEW_KEYS:
        return None
    if (
        not _is_price(result.get("totalPurchaseAmount"))
        or not _is_overview_market_value(result.get("marketValue"))
        or not _is_overview_profit_loss(result.get("profitLoss"))
        or not _is_overview_daily_profit_loss(result.get("dailyProfitLoss"))
    ):
        return None
    raw_items = result.get("items")
    if not isinstance(raw_items, list):
        return None
    positions: list[TossHoldingPosition] = []
    identities: set[tuple[str, str]] = set()
    for raw_item in cast(list[object], raw_items):
        position = _holding_position(raw_item)
        if position is None:
            return None
        identity = (position.market_country, position.symbol)
        if identity in identities:
            return None
        identities.add(identity)
        positions.append(position)
    return tuple(positions)


def _holding_position(value: object) -> TossHoldingPosition | None:
    if not isinstance(value, Mapping):
        return None
    item = cast(Mapping[str, object], value)
    if not item.keys() >= _HOLDINGS_ITEM_KEYS:
        return None
    symbol = item.get("symbol")
    name = item.get("name")
    market_country = item.get("marketCountry")
    currency = item.get("currency")
    raw_quantity = item.get("quantity")
    if (
        not isinstance(symbol, str)
        or not isinstance(name, str)
        or not name
        or "\n" in name
        or not isinstance(market_country, str)
        or not isinstance(currency, str)
        or not isinstance(raw_quantity, str)
        or not raw_quantity
        or len(raw_quantity) > 30
        or not raw_quantity.isascii()
        or not _is_decimal_text(item.get("lastPrice"))
        or not _is_decimal_text(item.get("averagePurchasePrice"))
        or not _is_decimal_record(
            item.get("marketValue"),
            required=("purchaseAmount", "amount", "amountAfterCost"),
        )
        or not _is_decimal_record(
            item.get("profitLoss"),
            required=("amount", "amountAfterCost", "rate", "rateAfterCost"),
        )
        or not _is_decimal_record(
            item.get("dailyProfitLoss"), required=("amount", "rate")
        )
        or not _is_decimal_record(
            item.get("cost"), required=("commission",), nullable_optional=("tax",)
        )
    ):
        return None
    try:
        quantity = parse_contract_decimal(raw_quantity)
        return TossHoldingPosition(
            symbol=symbol,
            market_country=market_country,
            currency=currency,
            total_quantity=quantity,
        )
    except TypeError, ValueError, ArithmeticError:
        return None


def _decode_sellable_quantity(status: int, body: bytes) -> Decimal | None:
    if status != 200:
        return None
    try:
        payload: object = json.loads(body)
    except UnicodeDecodeError, json.JSONDecodeError, RecursionError:
        return None
    if not isinstance(payload, Mapping):
        return None
    result = cast(Mapping[str, object], payload).get("result")
    if not isinstance(result, Mapping):
        return None
    raw_quantity = cast(Mapping[str, object], result).get("sellableQuantity")
    if (
        not isinstance(raw_quantity, str)
        or not raw_quantity
        or len(raw_quantity) > 30
        or not raw_quantity.isascii()
        or not raw_quantity.isdecimal()
    ):
        return None
    try:
        quantity = parse_contract_decimal(raw_quantity)
    except TypeError, ValueError, ArithmeticError:
        return None
    if quantity < 0 or quantity != quantity.to_integral_value():
        return None
    return quantity


def _is_price(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    price = cast(Mapping[str, object], value)
    return _is_decimal_text(price.get("krw")) and (
        "usd" not in price or _is_decimal_text(price.get("usd"), nullable=True)
    )


def _is_overview_market_value(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    record = cast(Mapping[str, object], value)
    return record.keys() >= {"amount", "amountAfterCost"} and all(
        _is_price(record[key]) for key in ("amount", "amountAfterCost")
    )


def _is_overview_profit_loss(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    record = cast(Mapping[str, object], value)
    return record.keys() >= {"amount", "amountAfterCost", "rate", "rateAfterCost"} and (
        _is_price(record["amount"])
        and _is_price(record["amountAfterCost"])
        and _is_decimal_text(record["rate"])
        and _is_decimal_text(record["rateAfterCost"])
    )


def _is_overview_daily_profit_loss(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    record = cast(Mapping[str, object], value)
    return (
        record.keys() >= {"amount", "rate"}
        and _is_price(record["amount"])
        and _is_decimal_text(record["rate"])
    )


def _is_decimal_record(
    value: object,
    *,
    required: tuple[str, ...],
    nullable_optional: tuple[str, ...] = (),
) -> bool:
    if not isinstance(value, Mapping):
        return False
    record = cast(Mapping[str, object], value)
    if not record.keys() >= set(required):
        return False
    return all(_is_decimal_text(record[key]) for key in required) and all(
        key not in record or _is_decimal_text(record[key], nullable=True)
        for key in nullable_optional
    )


def _is_decimal_text(value: object, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 30
        or not value.isascii()
    ):
        return False
    try:
        parse_contract_decimal(value)
    except TypeError, ValueError, ArithmeticError:
        return False
    return True


def _is_kr_domestic_symbol(value: object) -> bool:
    return (
        type(value) is str and len(value) == 6 and value.isascii() and value.isdecimal()
    )


def _is_us_symbol(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value.isascii()
        and all(character.isalnum() or character in ".-" for character in value)
    )
