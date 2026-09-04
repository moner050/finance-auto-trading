"""The price a market order can be expected to get, read when it is needed.

`OrderIntentFactory` refuses a market order with no trigger price unless it is
given a fresh side-specific quote, and its reason is the right one: such an
order goes to the market now, so the price it will get has to be known now. A
stop is different - it waits, and its trigger decides when it stops waiting.

Nothing supplied one. Entry and exit are both market orders with no trigger,
so neither could build an intent at all; the protective stop only got through
because it carries a trigger. §31.11.

Two things this deliberately does not do.

**It does not build a quote from a bar.** A close is one number, and putting
it in both `bid` and `ask` says the spread is zero - which is the thing the
rule exists to stop being assumed.

**It does not assume freshness.** `fresh` is measured: the round trip is
timed, and a read that took longer than the window says so. A price that
arrived late is a price that may already be gone.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta
from typing import Protocol

from autotrader.execution.intents.models import MarketQuote
from autotrader.integrations.market_data.binance_instrument import read_spread
from autotrader.integrations.market_data.binance_public_rest import BinancePublicRest


class QuoteSource(Protocol):
    async def quote(self) -> MarketQuote:
        """The best bid and ask, and whether acting on them now is honest."""
        ...


class BinanceBookQuotes:
    def __init__(
        self,
        *,
        rest: BinancePublicRest,
        symbol: str,
        within: timedelta = timedelta(seconds=2),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if within <= timedelta(0):
            raise ValueError("a quote freshness window must be positive")
        if not symbol or symbol != symbol.strip().upper():
            raise ValueError("a quote source needs one exact symbol")
        self._rest = rest
        self._symbol = symbol
        self._within = within.total_seconds()
        self._monotonic = monotonic

    async def quote(self) -> MarketQuote:
        started = self._monotonic()
        spread = await read_spread(self._rest, symbol=self._symbol)
        elapsed = self._monotonic() - started
        return MarketQuote(
            bid=spread.best_bid,
            ask=spread.best_ask,
            # Not a claim about the venue's clock, which this cannot see. It
            # says only that the answer came back fast enough that the price
            # in it is still the price being acted on.
            fresh=elapsed <= self._within,
        )


__all__ = ("BinanceBookQuotes", "QuoteSource")
