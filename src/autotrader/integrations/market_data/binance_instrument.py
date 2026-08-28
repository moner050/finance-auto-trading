"""The venue's own numbers for one instrument.

Tick size and lot size decide the price and the quantity of a real order.
Until now the only way to supply them was to type them somewhere, and a typed
number can be wrong while every order it shapes still looks reasonable. These
come from the exchange, over public endpoints that need no credentials, so the
loop can be told what it is trading before anyone has an account.

The spread is read the same way, from the best bid and ask, because a spread
is an observation about the book right now rather than a setting.

Nothing here is derived, averaged or defaulted. A filter the venue did not
send is a refusal: an order priced against an assumed tick size is a real
order priced wrongly.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from autotrader.integrations.market_data.binance_public_rest import (
    BinancePublicRest,
    BinancePublicRestError,
)

PRICE_FILTER = "PRICE_FILTER"
LOT_SIZE = "LOT_SIZE"
MIN_NOTIONAL = "MIN_NOTIONAL"


@dataclass(frozen=True, slots=True)
class InstrumentSpecification:
    """What the venue says an order in this instrument must look like."""

    symbol: str
    tick_size: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal

    def __post_init__(self) -> None:
        for name in (
            "tick_size",
            "quantity_step",
            "minimum_quantity",
            "minimum_notional",
        ):
            value = getattr(self, name)
            if type(value) is not Decimal or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")


@dataclass(frozen=True, slots=True)
class BookSpread:
    """The best bid and ask, and the distance between them."""

    symbol: str
    best_bid: Decimal
    best_ask: Decimal

    def __post_init__(self) -> None:
        for name in ("best_bid", "best_ask"):
            value = getattr(self, name)
            if type(value) is not Decimal or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
        if self.best_ask < self.best_bid:
            # A crossed book is not a spread we can size against.
            raise ValueError("the best ask cannot be below the best bid")

    @property
    def spread(self) -> Decimal:
        return self.best_ask - self.best_bid


async def read_specification(
    rest: BinancePublicRest, *, symbol: str
) -> InstrumentSpecification:
    """The venue's filters, or a refusal naming the one that was absent."""
    entry = await rest.exchange_info(symbol=symbol)
    if entry.get("symbol") != symbol:
        raise BinancePublicRestError("exchangeInfo answered for another symbol")
    filters = _filters(entry, symbol=symbol)
    return InstrumentSpecification(
        symbol=symbol,
        tick_size=_amount(filters, PRICE_FILTER, "tickSize", symbol=symbol),
        quantity_step=_amount(filters, LOT_SIZE, "stepSize", symbol=symbol),
        minimum_quantity=_amount(filters, LOT_SIZE, "minQty", symbol=symbol),
        minimum_notional=_amount(filters, MIN_NOTIONAL, "notional", symbol=symbol),
    )


async def read_spread(rest: BinancePublicRest, *, symbol: str) -> BookSpread:
    payload = await rest.book_ticker(symbol=symbol)
    if payload.get("symbol") != symbol:
        raise BinancePublicRestError("bookTicker answered for another symbol")
    return BookSpread(
        symbol=symbol,
        best_bid=_decimal(payload.get("bidPrice"), "bidPrice"),
        best_ask=_decimal(payload.get("askPrice"), "askPrice"),
    )


def _filters(entry: dict[str, object], *, symbol: str) -> dict[str, dict[str, object]]:
    found = entry.get("filters")
    if not isinstance(found, list):
        raise BinancePublicRestError(f"exchangeInfo sent no filters for {symbol}")
    collected: dict[str, dict[str, object]] = {}
    for item in found:  # type: ignore[assignment]
        if not isinstance(item, dict):
            continue
        name = item.get("filterType")  # type: ignore[union-attr]
        if isinstance(name, str):
            collected[name] = item  # type: ignore[assignment]
    return collected


def _amount(
    filters: dict[str, dict[str, object]],
    filter_type: str,
    field: str,
    *,
    symbol: str,
) -> Decimal:
    found = filters.get(filter_type)
    if found is None:
        raise BinancePublicRestError(
            f"exchangeInfo sent no {filter_type} for {symbol}; "
            "an order shaped without it would be shaped by a guess"
        )
    return _decimal(found.get(field), f"{filter_type}.{field}")


def _decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, str):
        raise BinancePublicRestError(f"{name} was not sent as a string")
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise BinancePublicRestError(f"{name} is not a number") from error


__all__ = (
    "LOT_SIZE",
    "MIN_NOTIONAL",
    "PRICE_FILTER",
    "BookSpread",
    "InstrumentSpecification",
    "read_specification",
    "read_spread",
)
