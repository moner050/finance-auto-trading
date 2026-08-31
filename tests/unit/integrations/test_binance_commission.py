"""Reading what the account pays, and refusing to guess it.

The rate is account-specific, so the failure worth pinning is the quiet one:
a response that is missing, wrong, or for another symbol turning into a
plausible number that a cost filter then agrees with.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from autotrader.integrations.brokers.common import BrokerRequest, BrokerResponse
from autotrader.integrations.market_data.binance_commission import (
    BinanceCommissionError,
    CommissionRates,
    fee_schedule_for,
    read_commission_rates,
)

# The rates this account was actually quoted, read from the live venue.
LIVE_MAKER = Decimal("0.000200")
LIVE_TAKER = Decimal("0.000500")


class _Transport:
    """Stands in for the signed transport, and records what it was asked."""

    def __init__(self, response: BrokerResponse) -> None:
        self._response = response
        self.requests: list[BrokerRequest] = []

    async def send(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return self._response


def _response(status: int = 200, **overrides: object) -> BrokerResponse:
    payload: dict[str, object] = {
        "symbol": "BTCUSDT",
        "makerCommissionRate": "0.000200",
        "takerCommissionRate": "0.000500",
    }
    payload.update(overrides)
    for key, value in tuple(payload.items()):
        if value is None:
            del payload[key]
    return BrokerResponse(status=status, body=json.dumps(payload).encode("utf-8"))


async def _read(transport: object) -> CommissionRates:
    return await read_commission_rates(transport, symbol="BTCUSDT")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_live_rates_are_read_as_sent() -> None:
    transport = _Transport(_response())

    rates = await _read(transport)

    assert rates == CommissionRates(
        symbol="BTCUSDT", maker=LIVE_MAKER, taker=LIVE_TAKER
    )
    assert transport.requests[0].path == "/fapi/v1/commissionRate?symbol=BTCUSDT"
    assert transport.requests[0].method == "GET"


@pytest.mark.asyncio
async def test_a_response_for_another_symbol_is_refused() -> None:
    """Binance ignores the symbol on some endpoints and answers with all of
    them, so a response naming a different one is not a hypothetical."""
    transport = _Transport(_response(symbol="ETHUSDT"))

    with pytest.raises(BinanceCommissionError, match="another symbol"):
        await _read(transport)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("makerCommissionRate", "takerCommissionRate"))
async def test_a_missing_rate_is_refused_rather_than_defaulted(field: str) -> None:
    transport = _Transport(_response(**{field: None}))

    with pytest.raises(BinanceCommissionError, match="incomplete"):
        await _read(transport)


@pytest.mark.asyncio
async def test_a_failed_request_names_its_status() -> None:
    transport = _Transport(BrokerResponse(status=401, body=b"{}"))

    with pytest.raises(BinanceCommissionError, match="401"):
        await _read(transport)


@pytest.mark.asyncio
async def test_a_body_that_is_not_json_is_refused() -> None:
    transport = _Transport(BrokerResponse(status=200, body=b"<html>"))

    with pytest.raises(BinanceCommissionError, match="not JSON"):
        await _read(transport)


@pytest.mark.asyncio
async def test_rates_read_into_the_wrong_fields_are_caught() -> None:
    """Maker above taker on this venue means the two were swapped, which is
    a mistake that otherwise produces a schedule that is simply too cheap."""
    transport = _Transport(
        _response(makerCommissionRate="0.000500", takerCommissionRate="0.000200")
    )

    with pytest.raises(BinanceCommissionError, match="below maker"):
        await _read(transport)


def test_both_legs_are_charged_at_the_taker_rate() -> None:
    """The entry may rest and earn the maker rate, but a limit order that
    crosses pays taker, and this feeds a filter deciding whether a trade
    clears its cost. Assuming the rebate lets trades through unpriced."""
    schedule = fee_schedule_for(
        CommissionRates(symbol="BTCUSDT", maker=LIVE_MAKER, taker=LIVE_TAKER),
        price=Decimal("60000"),
    )

    assert schedule.entry_fee_per_unit == Decimal("30.000000")
    assert schedule.exit_taker_fee_per_unit == Decimal("30.000000")


@pytest.mark.parametrize("price", (Decimal("0"), Decimal("-1")))
def test_a_non_positive_price_is_refused(price: Decimal) -> None:
    with pytest.raises(ValueError, match="positive"):
        fee_schedule_for(
            CommissionRates(symbol="BTCUSDT", maker=LIVE_MAKER, taker=LIVE_TAKER),
            price=price,
        )


def test_a_negative_rate_is_refused() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        CommissionRates(symbol="BTCUSDT", maker=Decimal("-0.0001"), taker=LIVE_TAKER)
