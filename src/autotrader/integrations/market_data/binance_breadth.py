"""How much of the market went up, and where that sits in its own history.

The strategy asks for a breadth percentile. It was written for equity index
futures, where breadth is the share of an index's constituents advancing. A
perpetual futures venue has no index, so the question is what set of contracts
counts as the market.

The venue's own answer is used: the contracts it lists as TRADING PERPETUAL
quoted in USDT. Picking a subset would be choosing which market the strategy
is reading, which is a judgement this module is not entitled to make.

Two things are said out loud rather than smoothed over.

A day is only counted when enough contracts traded on it. Early history has
few listings, and a share computed over eleven symbols is not the same
measurement as one computed over five hundred.

The constituent list is today's. Contracts delisted since are absent from it,
so historical breadth is measured over survivors and reads slightly high. That
is a property of computing history from a current listing, and the alternative
— storing breadth daily from now on — takes a year to become useful.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise

from autotrader.integrations.market_data.binance_public_rest import (
    BinancePublicRest,
    BinancePublicRestError,
)

TRADING = "TRADING"
PERPETUAL = "PERPETUAL"
QUOTE_ASSET = "USDT"

# Below this the share is a different measurement, not a noisier one.
MINIMUM_CONSTITUENTS = 50

# Binance weighs a klines request at two for these limits, and allows 2400 a
# minute. Eight at a time keeps a full sweep inside the budget without
# needing a token bucket for a job that runs once a day.
_CONCURRENCY = 8


@dataclass(frozen=True, slots=True)
class BreadthReading:
    """One day's advance-decline count."""

    exchange_date: date
    advancing: int
    declining: int
    unchanged: int

    def __post_init__(self) -> None:
        for name in ("advancing", "declining", "unchanged"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if type(self.exchange_date) is not date:
            raise TypeError("exchange_date must be an exact date")

    @property
    def constituents(self) -> int:
        return self.advancing + self.declining + self.unchanged

    @property
    def share_advancing(self) -> Decimal:
        """Unchanged contracts stay in the denominator.

        A day where nothing moved is a day nothing advanced, and dropping the
        flat ones would report it as though the market had risen.
        """
        total = self.constituents
        if total == 0:
            raise ValueError("a day with no constituents has no breadth")
        return Decimal(self.advancing) / Decimal(total)


def breadth_series(
    closes: Mapping[str, Sequence[tuple[date, Decimal]]],
) -> tuple[BreadthReading, ...]:
    """One reading per day, from each contract's own consecutive closes.

    A contract contributes to a day only when it also has the day before it.
    Comparing against the last close it happened to have would count a gap in
    the data as a price move.
    """
    counts: dict[date, list[int]] = {}
    for series in closes.values():
        ordered = sorted(series, key=lambda item: item[0])
        for (earlier_day, earlier), (day, close) in pairwise(ordered):
            if (day - earlier_day).days != 1:
                continue
            bucket = counts.setdefault(day, [0, 0, 0])
            if close > earlier:
                bucket[0] += 1
            elif close < earlier:
                bucket[1] += 1
            else:
                bucket[2] += 1
    return tuple(
        BreadthReading(
            exchange_date=day,
            advancing=bucket[0],
            declining=bucket[1],
            unchanged=bucket[2],
        )
        for day, bucket in sorted(counts.items())
    )


def breadth_percentile(
    series: Sequence[BreadthReading],
    *,
    minimum_constituents: int = MINIMUM_CONSTITUENTS,
) -> Decimal:
    """Where the latest day's breadth sits among the days before it.

    A rank, not a share: the strategy reads a low percentile as pessimism, and
    a raw share of 0.4 means nothing without knowing whether 0.4 is unusual
    for this market.
    """
    usable = [
        reading
        for reading in sorted(series, key=lambda item: item.exchange_date)
        if reading.constituents >= minimum_constituents
    ]
    if len(usable) < 2:
        raise ValueError(
            "a percentile needs a history; "
            f"{len(usable)} days had at least {minimum_constituents} contracts"
        )
    shares = [reading.share_advancing for reading in usable]
    today = shares[-1]
    at_or_below = sum(1 for share in shares if share <= today)
    return Decimal(at_or_below) / Decimal(len(shares))


async def read_universe(rest: BinancePublicRest) -> tuple[str, ...]:
    """The contracts the venue itself lists as its USDT perpetual market."""
    payload = await rest.exchange_info_all()
    found = payload.get("symbols")
    if not isinstance(found, list):
        raise BinancePublicRestError("exchangeInfo sent no symbols")
    selected: list[str] = []
    for item in found:  # type: ignore[assignment]
        if not isinstance(item, dict):
            continue
        entry: dict[str, object] = item  # type: ignore[assignment]
        if (
            entry.get("status") == TRADING
            and entry.get("contractType") == PERPETUAL
            and entry.get("quoteAsset") == QUOTE_ASSET
        ):
            symbol = entry.get("symbol")
            if isinstance(symbol, str):
                selected.append(symbol)
    if not selected:
        raise BinancePublicRestError("the venue listed no USDT perpetual contracts")
    return tuple(sorted(selected))


async def read_daily_closes(
    rest: BinancePublicRest, *, symbols: Sequence[str], days: int, now: datetime
) -> dict[str, tuple[tuple[date, Decimal], ...]]:
    """Daily closes per contract, fetched a few at a time."""
    if days <= 1:
        raise ValueError("a breadth series needs more than one day")
    end_ms = int(now.timestamp() * 1000)
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def one(symbol: str) -> tuple[str, tuple[tuple[date, Decimal], ...]]:
        async with gate:
            rows = await rest.klines(
                symbol=symbol, interval="1d", end_time_ms=end_ms, limit=days
            )
        return symbol, _closes(rows)

    collected = await asyncio.gather(*(one(symbol) for symbol in symbols))
    return {symbol: series for symbol, series in collected if series}


def _closes(rows: Sequence[object]) -> tuple[tuple[date, Decimal], ...]:
    found: list[tuple[date, Decimal]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 5:  # type: ignore[arg-type]
            continue
        values: list[object] = row  # type: ignore[assignment]
        open_ms, close = values[0], values[4]
        if not isinstance(open_ms, int) or not isinstance(close, str):
            continue
        found.append(
            (
                datetime.fromtimestamp(open_ms / 1000, tz=UTC).date(),
                Decimal(close),
            )
        )
    return tuple(found)


__all__ = (
    "MINIMUM_CONSTITUENTS",
    "PERPETUAL",
    "QUOTE_ASSET",
    "TRADING",
    "BreadthReading",
    "breadth_percentile",
    "breadth_series",
    "read_daily_closes",
    "read_universe",
)
