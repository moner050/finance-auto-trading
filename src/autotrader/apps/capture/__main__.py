"""Record the day's market measures.

    python -m autotrader.apps.capture

Run once a day, after the UTC day it measures has closed. It reads what the
two venues publish, writes the raw numbers, and says what it could not reach.

A venue being down costs that measure for that day and nothing else. The row
is written with what was observed, the gap stays a gap, and the percentile
that reads the series skips it rather than carrying yesterday forward — a
filled-in value would be counted as an observation in every rank afterwards.

Deribit publishes no daily history of the put-call ratio, so this is the only
way that series can exist. It starts the day this first runs.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.integrations.market_data.binance_breadth import (
    breadth_series,
    read_daily_closes,
    read_universe,
)
from autotrader.integrations.market_data.binance_public_rest import BinancePublicRest
from autotrader.integrations.market_data.deribit_put_call import (
    DeribitPublic,
    read_put_call,
)
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.repositories.pessimism import (
    DailyMeasures,
    MarketPessimism,
)
from autotrader.strategies.david_v6.regime import PessimismInputs

USAGE = "usage: python -m autotrader.apps.capture [--days N] [--backfill]"

# Enough history for a thirty-day realised volatility and a breadth series
# that reaches back past the newest listings.
DEFAULT_DAYS = 120
VOLATILITY_WINDOW = 30
SYMBOL = "BTCUSDT"


def realised_volatility(closes: list[Decimal], *, window: int) -> Decimal | None:
    """The standard deviation of the last window of daily returns."""
    if len(closes) <= window:
        return None
    returns = [
        float((later - earlier) / earlier) for earlier, later in pairwise(closes)
    ]
    return Decimal(repr(statistics.pstdev(returns[-window:])))


def volatility_by_day(
    closes: Sequence[tuple[date, Decimal]], *, window: int
) -> dict[date, Decimal]:
    """Realised volatility as it stood at the end of each day.

    Every day is ranked against the days before it, so every day needs its own
    value. Computing one number and attaching it to all of them would rank a
    series of identical readings and call the result a percentile.
    """
    ordered = sorted(closes, key=lambda item: item[0])
    returns = [
        (day, float((later - earlier) / earlier))
        for (_, earlier), (day, later) in pairwise(ordered)
    ]
    found: dict[date, Decimal] = {}
    for index in range(window, len(returns) + 1):
        values = [value for _, value in returns[index - window : index]]
        found[returns[index - 1][0]] = Decimal(repr(statistics.pstdev(values)))
    return found


async def _measure(days: int) -> tuple[DailyMeasures, tuple[str, ...]]:
    """Everything the venues would answer, and what they would not."""
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    unreachable: list[str] = []
    volatility: Decimal | None = None
    advancing = declining = unchanged = None
    calls = puts = None

    try:
        async with BinancePublicRest() as rest:
            symbols = await read_universe(rest)
            closes = await read_daily_closes(
                rest, symbols=symbols, days=days, now=datetime.now(UTC)
            )
        btc = [close for _, close in sorted(closes.get(SYMBOL, ()))]
        volatility = realised_volatility(btc, window=VOLATILITY_WINDOW)
        for reading in breadth_series(closes):
            if reading.exchange_date == yesterday:
                advancing = reading.advancing
                declining = reading.declining
                unchanged = reading.unchanged
                break
        if advancing is None:
            unreachable.append(f"breadth: no complete day for {yesterday}")
    except Exception as error:
        unreachable.append(f"binance: {error}")

    try:
        async with DeribitPublic() as deribit:
            reading = await read_put_call(deribit, now=datetime.now(UTC))
        calls, puts = reading.calls_volume, reading.puts_volume
    except Exception as error:
        unreachable.append(f"deribit: {error}")

    if volatility is None and advancing is None and calls is None:
        raise RuntimeError("nothing could be measured:\n  " + "\n  ".join(unreachable))
    return (
        DailyMeasures(
            exchange_date=yesterday,
            realised_volatility=volatility,
            breadth_advancing=advancing,
            breadth_declining=declining,
            breadth_unchanged=unchanged,
            calls_volume=calls,
            puts_volume=puts,
        ),
        tuple(unreachable),
    )


async def backfill(days: int) -> int:
    """Reconstruct the days the venues still publish.

    Breadth and realised volatility can be rebuilt from klines at any time.
    The put-call ratio cannot: Deribit publishes no daily history, so
    backfilled days carry no put-call reading and a day already recorded is
    left exactly as it is.
    """
    now = datetime.now(UTC)
    async with BinancePublicRest() as rest:
        symbols = await read_universe(rest)
        closes = await read_daily_closes(rest, symbols=symbols, days=days, now=now)
    volatility = volatility_by_day(closes.get(SYMBOL, ()), window=VOLATILITY_WINDOW)
    readings = breadth_series(closes)
    yesterday = (now - timedelta(days=1)).date()

    settings = Settings()
    engine = create_engine(settings)
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    written = skipped = 0
    try:
        async with sessions() as session:
            repository = MarketPessimism(session)
            for reading in readings:
                if reading.exchange_date > yesterday:
                    # A day that has not closed is not a measurement of it.
                    continue
                measures = DailyMeasures(
                    exchange_date=reading.exchange_date,
                    realised_volatility=volatility.get(reading.exchange_date),
                    breadth_advancing=reading.advancing,
                    breadth_declining=reading.declining,
                    breadth_unchanged=reading.unchanged,
                )
                if await repository.record_if_absent(measures, now=now):
                    written += 1
                else:
                    skipped += 1
            await session.commit()
        async with sessions() as session:
            pessimism = await MarketPessimism(session).pessimism(through=yesterday)
            await session.rollback()
    finally:
        await engine.dispose()

    print(f"backfilled {written} days, left {skipped} already recorded")
    print("percentiles:")
    _print_percentiles(pessimism)
    print(
        "the put-call series is not backfilled; Deribit publishes no history "
        "of it, so it starts the day the daily capture first runs."
    )
    return 0


def _print_percentiles(pessimism: PessimismInputs) -> None:
    for name, value in (
        ("volatility", pessimism.volatility_percentile),
        ("breadth", pessimism.breadth_percentile),
        ("put-call", pessimism.put_call_percentile),
    ):
        print(f"  {name:<12} {value if value is not None else 'not enough history'}")


async def capture(days: int) -> int:
    measures, unreachable = await _measure(days)
    settings = Settings()
    engine = create_engine(settings)
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            repository = MarketPessimism(session)
            await repository.record(measures, now=datetime.now(UTC))
            await session.commit()
        async with sessions() as session:
            pessimism = await MarketPessimism(session).pessimism(
                through=measures.exchange_date
            )
            history = len(
                await MarketPessimism(session).series(through=measures.exchange_date)
            )
            await session.rollback()
    finally:
        await engine.dispose()

    print(f"recorded {measures.exchange_date}")
    print(f"  realised volatility {measures.realised_volatility}")
    print(f"  breadth             {_breadth(measures)}")
    print(f"  put-call            {_put_call(measures)}")
    print(f"days recorded so far  {history}")
    print("percentiles:")
    _print_percentiles(pessimism)
    for reason in unreachable:
        print(f"unreachable: {reason}", file=sys.stderr)
    return 0


def _breadth(measures: DailyMeasures) -> str:
    share = measures.breadth_share
    if share is None:
        return "absent"
    return (
        f"{measures.breadth_advancing}/{measures.breadth_declining}/"
        f"{measures.breadth_unchanged} = {share:.4f}"
    )


def _put_call(measures: DailyMeasures) -> str:
    ratio = measures.put_call_ratio
    return "absent" if ratio is None else f"{ratio:.4f}"


def _days(argv: tuple[str, ...]) -> int | None:
    for index, item in enumerate(argv):
        if item == "--days" and index + 1 < len(argv):
            try:
                return int(argv[index + 1])
            except ValueError:
                return None
    return DEFAULT_DAYS


def main(argv: tuple[str, ...]) -> int:
    days = _days(argv)
    if days is None or days <= VOLATILITY_WINDOW:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        if "--backfill" in argv:
            return asyncio.run(backfill(days))
        return asyncio.run(capture(days))
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))


__all__ = (
    "DEFAULT_DAYS",
    "VOLATILITY_WINDOW",
    "backfill",
    "capture",
    "main",
    "realised_volatility",
    "volatility_by_day",
)
