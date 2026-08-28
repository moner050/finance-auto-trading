"""Reading the venue's numbers instead of typing them.

Tick size and lot size shape a real order's price and quantity. The tests that
matter are the ones checking that an absent filter stops the read rather than
becoming a default, because a default here is a guess wearing the venue's
authority.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from autotrader.integrations.market_data.binance_instrument import (
    BookSpread,
    InstrumentSpecification,
    read_specification,
    read_spread,
)
from autotrader.integrations.market_data.binance_public_rest import (
    BinancePublicRestError,
)

SYMBOL = "BTCUSDT"

FILTERS = (
    {"filterType": "PRICE_FILTER", "tickSize": "0.10", "minPrice": "556.80"},
    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
    {"filterType": "MIN_NOTIONAL", "notional": "100"},
)


class _Rest:
    """Answers with a recorded shape, so no test reaches the network."""

    def __init__(
        self,
        *,
        info: dict[str, object] | None = None,
        ticker: dict[str, object] | None = None,
    ) -> None:
        self._info = info
        self._ticker = ticker

    async def exchange_info(self, *, symbol: str) -> dict[str, object]:
        del symbol
        if self._info is None:
            raise AssertionError("this scenario does not read exchange info")
        return self._info

    async def book_ticker(self, *, symbol: str) -> dict[str, object]:
        del symbol
        if self._ticker is None:
            raise AssertionError("this scenario does not read a book ticker")
        return self._ticker


def _info(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {"symbol": SYMBOL, "filters": list(FILTERS)}
    payload.update(changes)
    return payload


def _read(rest: _Rest) -> InstrumentSpecification:
    return asyncio.run(read_specification(rest, symbol=SYMBOL))  # type: ignore[arg-type]


def test_the_filters_become_the_specification() -> None:
    found = _read(_Rest(info=_info()))

    assert found.tick_size == Decimal("0.10")
    assert found.quantity_step == Decimal("0.001")
    assert found.minimum_quantity == Decimal("0.001")
    assert found.minimum_notional == Decimal("100")


@pytest.mark.parametrize("absent", ("PRICE_FILTER", "LOT_SIZE", "MIN_NOTIONAL"))
def test_an_absent_filter_stops_the_read(absent: str) -> None:
    """Not a default. An order shaped without it is shaped by a guess."""
    remaining = [item for item in FILTERS if item["filterType"] != absent]

    with pytest.raises(BinancePublicRestError, match=absent):
        _read(_Rest(info=_info(filters=remaining)))


def test_an_answer_about_another_symbol_is_refused() -> None:
    with pytest.raises(BinancePublicRestError, match="another symbol"):
        _read(_Rest(info=_info(symbol="ETHUSDT")))


def test_a_filter_list_that_is_not_a_list_is_refused() -> None:
    with pytest.raises(BinancePublicRestError, match="no filters"):
        _read(_Rest(info=_info(filters="none")))


def test_a_numeric_field_sent_as_a_number_is_refused() -> None:
    """Binance sends these as strings. A float would already have lost
    precision by the time it arrived."""
    broken = [
        {"filterType": "PRICE_FILTER", "tickSize": 0.1},
        *(item for item in FILTERS if item["filterType"] != "PRICE_FILTER"),
    ]

    with pytest.raises(BinancePublicRestError, match="not sent as a string"):
        _read(_Rest(info=_info(filters=broken)))


def test_a_zero_tick_size_is_refused() -> None:
    broken = [
        {"filterType": "PRICE_FILTER", "tickSize": "0"},
        *(item for item in FILTERS if item["filterType"] != "PRICE_FILTER"),
    ]

    with pytest.raises(ValueError, match="positive finite"):
        _read(_Rest(info=_info(filters=broken)))


def test_the_spread_is_the_distance_between_the_best_prices() -> None:
    found = asyncio.run(
        read_spread(
            _Rest(  # type: ignore[arg-type]
                ticker={
                    "symbol": SYMBOL,
                    "bidPrice": "60000.10",
                    "askPrice": "60000.30",
                }
            ),
            symbol=SYMBOL,
        )
    )

    assert found.spread == Decimal("0.20")


def test_a_crossed_book_is_not_a_spread() -> None:
    with pytest.raises(ValueError, match="below the best bid"):
        BookSpread(symbol=SYMBOL, best_bid=Decimal("100"), best_ask=Decimal("99"))


def test_a_locked_book_has_no_spread_but_is_readable() -> None:
    """Bid equal to ask happens. It is zero, not an error."""
    found = BookSpread(symbol=SYMBOL, best_bid=Decimal("100"), best_ask=Decimal("100"))

    assert found.spread == Decimal(0)
