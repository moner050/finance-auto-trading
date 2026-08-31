"""What this account actually pays to trade.

The cost model is per unit, so it needs a rate and a price, and the rate is
not a property of the venue: it depends on VIP tier, on referral, and on
whether fees are paid in BNB. Two accounts on the same symbol pay different
numbers. That is why it was the last input with no source - it is readable
only from an authenticated endpoint, and until credentials existed there was
nothing to read.

Both legs are charged at the taker rate. The exit leg is a stop, which is
never anything else; the entry leg may rest and earn the maker rate, but a
limit order that crosses pays taker, and this number feeds a filter that
decides whether a trade clears its own cost. An estimate that assumes the
maker rate and then takes is an estimate that lets trades through which
should have been refused, so the cheaper rate is reported and not used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import cast

from autotrader.integrations.brokers.binance_usdm.transport import BinanceUsdmTransport
from autotrader.integrations.brokers.common import BrokerRequest
from autotrader.strategies.david_v6.costs import FeeSchedule

COMMISSION_RATE_PATH = "/fapi/v1/commissionRate"


class BinanceCommissionError(RuntimeError):
    """Raised when the account's commission rate cannot be read."""


@dataclass(frozen=True, slots=True)
class CommissionRates:
    """The two rates the venue charges this account on this symbol."""

    symbol: str
    maker: Decimal
    taker: Decimal

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        for name in ("maker", "taker"):
            value = getattr(self, name)
            if type(value) is not Decimal or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a non-negative finite Decimal")
        if self.taker < self.maker:
            # Not impossible in principle, but on this venue it would mean the
            # response was read into the wrong fields.
            raise ValueError("taker rate below maker rate is not expected")


async def read_commission_rates(
    transport: BinanceUsdmTransport, *, symbol: str
) -> CommissionRates:
    """The account's rates for one symbol, or a refusal.

    Signed, because the answer is account-specific. Nothing here falls back to
    a published rate card: a guessed fee makes a cost filter agree with trades
    it has not actually priced.
    """
    if not symbol or not symbol.isalnum():
        raise ValueError("symbol must be alphanumeric")
    response = await transport.send(
        BrokerRequest(method="GET", path=f"{COMMISSION_RATE_PATH}?symbol={symbol}")
    )
    if response.status != 200:
        raise BinanceCommissionError(
            f"commission rate request failed with status {response.status}"
        )
    try:
        decoded = json.loads(response.body)
    except ValueError as error:
        raise BinanceCommissionError("commission rate response is not JSON") from error
    if not isinstance(decoded, dict):
        raise BinanceCommissionError("commission rate response is not an object")
    payload = cast("dict[str, object]", decoded)
    if payload.get("symbol") != symbol:
        raise BinanceCommissionError("commission rate response is for another symbol")
    try:
        maker = Decimal(str(payload["makerCommissionRate"]))
        taker = Decimal(str(payload["takerCommissionRate"]))
    except (KeyError, TypeError, InvalidOperation) as error:
        raise BinanceCommissionError(
            "commission rate response is incomplete"
        ) from error
    try:
        return CommissionRates(symbol=symbol, maker=maker, taker=taker)
    except ValueError as error:
        raise BinanceCommissionError(str(error)) from error


def fee_schedule_for(rates: CommissionRates, *, price: Decimal) -> FeeSchedule:
    """The per-unit schedule the cost model wants, at a reference price.

    A rate is a fraction of notional and the model is per unit, so the price
    has to be the one the order would be filled near. Passing yesterday's
    close would price today's fees wrongly by however far the market has
    moved.
    """
    if type(rates) is not CommissionRates:
        raise TypeError("rates must be an exact CommissionRates")
    if type(price) is not Decimal or not price.is_finite() or price <= 0:
        raise ValueError("price must be a positive finite Decimal")
    per_unit = rates.taker * price
    return FeeSchedule(
        entry_fee_per_unit=per_unit,
        exit_taker_fee_per_unit=per_unit,
    )


__all__ = (
    "COMMISSION_RATE_PATH",
    "BinanceCommissionError",
    "CommissionRates",
    "fee_schedule_for",
    "read_commission_rates",
)
