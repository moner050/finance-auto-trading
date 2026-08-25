from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from uuid import UUID
from zoneinfo import ZoneInfo

from autotrader.domain.enums import OrderStyle, Side
from autotrader.shared.decimal import decimal_to_string, require_decimal

_KST = ZoneInfo("Asia/Seoul")


class KisRecoveryStatus(StrEnum):
    ADOPTED = "ADOPTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class KisAmbiguousDispatch:
    dispatch_id: UUID
    binding_id: UUID
    side: Side
    symbol: str
    order_style: OrderStyle
    quantity: Decimal
    limit_price: Decimal | None
    provider_window_start: datetime
    provider_window_end: datetime
    request_digest: bytes

    def __post_init__(self) -> None:
        _uuid7(self.dispatch_id, "dispatch_id")
        _uuid7(self.binding_id, "binding_id")
        if type(self.side) is not Side or type(self.order_style) is not OrderStyle:
            raise TypeError("dispatch side and order style must be exact enums")
        _symbol(self.symbol)
        quantity = _positive_integer(self.quantity, "quantity")
        price = _order_price(self.order_style, self.limit_price)
        _utc_second(self.provider_window_start, "provider_window_start")
        _utc_second(self.provider_window_end, "provider_window_end")
        if self.provider_window_start > self.provider_window_end:
            raise ValueError("provider recovery window is invalid")
        _digest(self.request_digest, "request_digest")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "limit_price", price)


@dataclass(frozen=True, slots=True)
class KisDailyOrder:
    binding_id: UUID
    order_date: str
    organization_number: str
    order_number: str
    original_order_number: str
    provider_timestamp: datetime
    side: Side
    symbol: str
    order_style: OrderStyle
    order_quantity: Decimal
    limit_price: Decimal | None
    cumulative_filled_quantity: Decimal
    average_fill_price: Decimal
    total_filled_amount: Decimal
    confirmed_cancelled_quantity: Decimal
    remaining_quantity: Decimal
    rejected_quantity: Decimal
    fee_amount: Decimal | None
    exchange_division_code: str = "00"
    exchange_id_division_code: str = "KRX"

    def __post_init__(self) -> None:
        _uuid7(self.binding_id, "binding_id")
        _digits(self.order_date, 8, "order_date")
        _digits(self.organization_number, 5, "organization_number")
        _digits(self.order_number, 10, "order_number")
        _digits(self.original_order_number, 10, "original_order_number")
        _utc_second(self.provider_timestamp, "provider_timestamp")
        if (
            self.provider_timestamp.astimezone(_KST).strftime("%Y%m%d")
            != self.order_date
        ):
            raise ValueError("provider timestamp and order date do not match")
        if type(self.side) is not Side or type(self.order_style) is not OrderStyle:
            raise TypeError("daily order side and style must be exact enums")
        _symbol(self.symbol)
        _digits(self.exchange_division_code, 2, "exchange_division_code")
        if self.exchange_id_division_code != "KRX":
            raise ValueError("exchange_id_division_code must be KRX")
        order_quantity = _positive_integer(self.order_quantity, "order_quantity")
        limit_price = _order_price(self.order_style, self.limit_price)
        filled = _non_negative_integer(
            self.cumulative_filled_quantity, "cumulative_filled_quantity"
        )
        average = _non_negative_integer(self.average_fill_price, "average_fill_price")
        total = _non_negative_integer(self.total_filled_amount, "total_filled_amount")
        cancelled = _non_negative_integer(
            self.confirmed_cancelled_quantity, "confirmed_cancelled_quantity"
        )
        remaining = _non_negative_integer(self.remaining_quantity, "remaining_quantity")
        rejected = _non_negative_integer(self.rejected_quantity, "rejected_quantity")
        fee = (
            None
            if self.fee_amount is None
            else _non_negative_integer(self.fee_amount, "fee_amount")
        )
        if filled + cancelled + remaining + rejected != order_quantity:
            raise ValueError("daily order quantity accounting is inconsistent")
        if (filled == 0 and (average != 0 or total != 0)) or (
            filled > 0 and (average <= 0 or total != average * filled)
        ):
            raise ValueError("daily order cumulative fill values are inconsistent")
        object.__setattr__(self, "order_quantity", order_quantity)
        object.__setattr__(self, "limit_price", limit_price)
        object.__setattr__(self, "cumulative_filled_quantity", filled)
        object.__setattr__(self, "average_fill_price", average)
        object.__setattr__(self, "total_filled_amount", total)
        object.__setattr__(self, "confirmed_cancelled_quantity", cancelled)
        object.__setattr__(self, "remaining_quantity", remaining)
        object.__setattr__(self, "rejected_quantity", rejected)
        object.__setattr__(self, "fee_amount", fee)

    @property
    def provider_identity(self) -> tuple[str, str, str]:
        return self.order_date, self.organization_number, self.order_number

    @property
    def record_digest(self) -> bytes:
        payload = {
            "averageFillPrice": decimal_to_string(self.average_fill_price),
            "bindingId": self.binding_id.hex,
            "confirmedCancelledQuantity": decimal_to_string(
                self.confirmed_cancelled_quantity
            ),
            "cumulativeFilledQuantity": decimal_to_string(
                self.cumulative_filled_quantity
            ),
            "feeAmount": (
                None if self.fee_amount is None else decimal_to_string(self.fee_amount)
            ),
            "exchangeDivisionCode": self.exchange_division_code,
            "exchangeIdDivisionCode": self.exchange_id_division_code,
            "limitPrice": (
                None
                if self.limit_price is None
                else decimal_to_string(self.limit_price)
            ),
            "orderDate": self.order_date,
            "orderNumber": self.order_number,
            "orderQuantity": decimal_to_string(self.order_quantity),
            "orderStyle": self.order_style.value,
            "organizationNumber": self.organization_number,
            "originalOrderNumber": self.original_order_number,
            "providerTimestamp": self.provider_timestamp.isoformat(),
            "rejectedQuantity": decimal_to_string(self.rejected_quantity),
            "remainingQuantity": decimal_to_string(self.remaining_quantity),
            "side": self.side.value,
            "symbol": self.symbol,
            "totalFilledAmount": decimal_to_string(self.total_filled_amount),
        }
        return sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).digest()


@dataclass(frozen=True, slots=True)
class KisRecoveryDecision:
    status: KisRecoveryStatus
    reason: str
    adopted_order: KisDailyOrder | None
    compared_candidate_digests: tuple[bytes, ...]

    def __post_init__(self) -> None:
        if type(self.status) is not KisRecoveryStatus:
            raise TypeError("recovery status must be exact")
        if self.status is KisRecoveryStatus.ADOPTED:
            if self.reason != "UNIQUE_EXACT_CANDIDATE" or self.adopted_order is None:
                raise ValueError("adopted recovery decision is incomplete")
        elif self.adopted_order is not None or self.reason not in {
            "ZERO_EXACT_CANDIDATES",
            "NON_UNIQUE_EXACT_CANDIDATES",
        }:
            raise ValueError("unknown recovery decision is invalid")
        for digest in self.compared_candidate_digests:
            _digest(digest, "compared_candidate_digest")


async def recover_ambiguous_cash_order(
    dispatch: KisAmbiguousDispatch,
    orders: Sequence[KisDailyOrder],
) -> KisRecoveryDecision:
    if type(dispatch) is not KisAmbiguousDispatch:
        raise TypeError("exact ambiguous KIS dispatch is required")
    dispatch.__post_init__()
    raw_orders = tuple(orders)
    if any(type(order) is not KisDailyOrder for order in raw_orders):
        raise TypeError("recovery orders must be exact KisDailyOrder values")
    candidates = raw_orders
    for order in candidates:
        order.__post_init__()
    exact = tuple(order for order in candidates if _matches(dispatch, order))
    compared = tuple(order.record_digest for order in candidates)
    if len(exact) == 1:
        return KisRecoveryDecision(
            status=KisRecoveryStatus.ADOPTED,
            reason="UNIQUE_EXACT_CANDIDATE",
            adopted_order=exact[0],
            compared_candidate_digests=compared,
        )
    return KisRecoveryDecision(
        status=KisRecoveryStatus.UNKNOWN,
        reason=(
            "ZERO_EXACT_CANDIDATES" if not exact else "NON_UNIQUE_EXACT_CANDIDATES"
        ),
        adopted_order=None,
        compared_candidate_digests=compared,
    )


def _matches(dispatch: KisAmbiguousDispatch, order: KisDailyOrder) -> bool:
    return (
        order.binding_id == dispatch.binding_id
        and order.side is dispatch.side
        and order.symbol == dispatch.symbol
        and order.order_style is dispatch.order_style
        and order.order_quantity == dispatch.quantity
        and order.limit_price == dispatch.limit_price
        and dispatch.provider_window_start
        <= order.provider_timestamp
        <= dispatch.provider_window_end
    )


def _order_price(style: OrderStyle, value: object) -> Decimal | None:
    if style is OrderStyle.MARKET:
        if value is not None:
            raise ValueError("market order must not have a limit price")
        return None
    if style is OrderStyle.LIMIT:
        if value is None:
            raise ValueError("limit order requires a limit price")
        return _positive_integer(value, "limit_price")
    raise ValueError("KIS cash recovery supports market and limit orders only")


def _positive_integer(value: object, name: str) -> Decimal:
    result = _non_negative_integer(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _non_negative_integer(value: object, name: str) -> Decimal:
    try:
        result = require_decimal(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite Decimal") from error
    if result < 0 or result != result.to_integral_value():
        raise ValueError(f"{name} must be a non-negative integer")
    return result


def _uuid7(value: object, name: str) -> UUID:
    if type(value) is not UUID or value.version != 7:
        raise ValueError(f"{name} must be UUIDv7")
    return value


def _utc_second(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.microsecond != 0
    ):
        raise ValueError(f"{name} must be whole-second timezone-aware")
    return value


def _digits(value: object, width: int, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != width
        or not value.isascii()
        or not value.isdecimal()
    ):
        raise ValueError(f"{name} must be {width} ASCII digits")
    return value


def _symbol(value: object) -> str:
    return _digits(value, 6, "symbol")


def _digest(value: object, name: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        raise ValueError(f"{name} must be a 32-byte SHA-256 digest")
    return value


__all__ = (
    "KisAmbiguousDispatch",
    "KisDailyOrder",
    "KisRecoveryDecision",
    "KisRecoveryStatus",
    "recover_ambiguous_cash_order",
)
