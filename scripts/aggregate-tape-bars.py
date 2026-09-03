"""Build sub-minute bars from the aggregate-trade tape, in the replay's format.

§13.2 lists `execution_scale: [1m, 30s, 5s]` as a free parameter. The klines
endpoint stops at one minute, so thirty and five seconds have to be aggregated
from the tape - which is why the tape was switched on.

Two things this does not do, and both are deliberate.

It does not reuse `BinanceUsdmMarketData._aggregate_bars`. That one stamps a
bar with its **close** time; the kline caches the replay reads stamp the
**open** time, and `replay-h0.py` builds `CompletedOhlcvBar` straight from
`row[0]`. The same class means different things in the two places, and a
thirty-second series stamped the production way would sit one bar to the
right of every five-minute series it is compared against. This follows the
replay's convention because the replay is what reads the output.

It does not invent a bar for a bucket with no trades. A quiet thirty seconds
and a gap in collection look identical once a flat bar is written, and the
tape has real gaps - a process restart, a lease handover. `--verify` is how
those get found rather than averaged over.

    python scripts/aggregate-tape-bars.py --symbol BTCUSDT --seconds 30
    python scripts/aggregate-tape-bars.py --symbol BTCUSDT --seconds 30 --verify
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine

ROOT = Path(__file__).resolve().parents[1]
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Read the tape in slices. Two days of BTCUSDT is over a million rows, and a
# single SELECT of the whole thing is a memory problem rather than a query.
PAGE = timedelta(hours=1)


def epoch_ms(moment: datetime) -> int:
    return int((moment - EPOCH) / timedelta(milliseconds=1))


async def tape_span(sessions, symbol: str) -> tuple[datetime, datetime] | None:
    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT MIN(occurred_at) lo, MAX(occurred_at) hi "
                    "FROM market_binance_usdm_trade WHERE symbol = :s"
                ),
                {"s": symbol},
            )
        ).one()
    if row.lo is None:
        return None
    return row.lo.replace(tzinfo=UTC), row.hi.replace(tzinfo=UTC)


async def aggregate(
    sessions, symbol: str, seconds: int, start: datetime, end: datetime
) -> list[list[object]]:
    """One row per bucket that had at least one trade, ordered by open time."""
    duration_ms = seconds * 1000
    rows: list[list[object]] = []
    cursor = start
    while cursor < end:
        stop = min(cursor + PAGE, end)
        async with sessions() as session:
            trades = (
                await session.execute(
                    text(
                        "SELECT occurred_at, price, quantity "
                        "FROM market_binance_usdm_trade "
                        "WHERE symbol = :s AND occurred_at >= :lo "
                        "AND occurred_at < :hi "
                        "ORDER BY occurred_at, CAST(provider_trade_id AS UNSIGNED)"
                    ),
                    {"s": symbol, "lo": cursor, "hi": stop},
                )
            ).all()
        buckets: dict[int, list[tuple[Decimal, Decimal]]] = {}
        for trade in trades:
            moment = trade.occurred_at.replace(tzinfo=UTC)
            opened = (epoch_ms(moment) // duration_ms) * duration_ms
            buckets.setdefault(opened, []).append((trade.price, trade.quantity))
        for opened in sorted(buckets):
            values = buckets[opened]
            prices = [price for price, _ in values]
            rows.append(
                [
                    opened,
                    str(prices[0]),
                    str(max(prices)),
                    str(min(prices)),
                    str(prices[-1]),
                    str(sum((quantity for _, quantity in values), Decimal(0))),
                ]
            )
        print(f"  {cursor:%Y-%m-%d %H:%M} → {len(rows):,} bars", flush=True)
        cursor = stop
    return rows


def verify(rows: list[list[object]], seconds: int, symbol: str, sample: int) -> None:
    """Compare a sample of windows against a direct aggregate-trade fetch.

    The first version of this compared against one-minute klines, and the
    klines are not comparable. Two measurements, both on 2026-09-01:

    - 07:42 volume. The tape holds 1,015 trades summing to 117.258, a direct
      fetch of the same ids returns the same 1,015 and the same 117.258, and
      the kline says 117.256.
    - 07:43 open. The tape's first trade is id 3435338552 at 07:43:00.148 for
      78576.80, a direct fetch by time returns exactly that trade first, and
      the kline's open is 78576.70 - the previous minute's close.

    So Binance's klines disagree with Binance's aggregate trades on both
    price and volume, and a check against them measures that rather than the
    tape. The aggregate-trade endpoint is the same source the tape is built
    from, which makes it the one thing that can confirm the tape is faithful
    and the buckets are drawn where the venue draws them.
    """
    import random

    import httpx

    if not rows:
        print("검증할 봉이 없다")
        return
    windows = sorted({(int(row[0]) // 60_000) * 60_000 for row in rows})
    # Skip the first and last minute: collection started and stopped inside
    # them, so a partial bucket there is expected rather than a finding.
    windows = windows[1:-1]
    if not windows:
        print("검증할 창이 없다")
        return
    chosen = sorted(random.Random(0).sample(windows, min(sample, len(windows))))
    folded: dict[int, list[list[object]]] = {}
    for row in rows:
        folded.setdefault((int(row[0]) // 60_000) * 60_000, []).append(row)

    agree = 0
    disagree: list[tuple[int, str]] = []
    with httpx.Client(timeout=30) as client:
        for opened in chosen:
            response = client.get(
                "https://fapi.binance.com/fapi/v1/aggTrades",
                params={
                    "symbol": symbol,
                    "startTime": opened,
                    "endTime": opened + 59_999,
                    "limit": 1000,
                },
            )
            response.raise_for_status()
            page = response.json()
            if len(page) >= 1000:
                # The window is busier than one page; a partial answer would
                # compare our whole minute against part of theirs.
                continue
            theirs = [(Decimal(str(x["p"])), Decimal(str(x["q"]))) for x in page]
            if not theirs:
                continue
            parts = folded.get(opened, [])
            ours = (
                Decimal(str(parts[0][1])),
                max(Decimal(str(part[2])) for part in parts),
                min(Decimal(str(part[3])) for part in parts),
                Decimal(str(parts[-1][4])),
                sum((Decimal(str(part[5])) for part in parts), Decimal(0)),
            )
            prices = [price for price, _ in theirs]
            mirror = (
                prices[0],
                max(prices),
                min(prices),
                prices[-1],
                sum((quantity for _, quantity in theirs), Decimal(0)),
            )
            for name, mine, yours in zip(
                ("open", "high", "low", "close", "volume"),
                ours,
                mirror,
                strict=True,
            ):
                if mine != yours:
                    disagree.append((opened, f"{name} {mine} != {yours}"))
                    break
            else:
                agree += 1
            time.sleep(0.2)

    def when(ms: int) -> str:
        return (EPOCH + timedelta(milliseconds=ms)).strftime("%m-%d %H:%M")

    print()
    print(f"원본 aggTrades 와 대조한 분: {agree + len(disagree)}")
    print(f"  완전히 일치: {agree}")
    print(f"  어긋남: {len(disagree)}")
    for opened, detail in disagree[:5]:
        print(f"    {when(opened)}  {detail}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--out", default=None)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--sample", type=int, default=20)
    arguments = parser.parse_args()
    if arguments.seconds <= 0 or 60 % arguments.seconds:
        raise SystemExit("--seconds must divide a minute")

    engine = create_engine(Settings())
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        span = await tape_span(sessions, arguments.symbol)
        if span is None:
            raise SystemExit(f"{arguments.symbol} 테이프가 비어 있다")
        start, end = span
        print(
            f"{arguments.symbol} 테이프 {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M}"
        )
        rows = await aggregate(
            sessions, arguments.symbol, arguments.seconds, start, end
        )
    finally:
        await engine.dispose()

    name = arguments.out or (
        f"build/klines-{arguments.symbol}-{arguments.seconds}s-tape.json"
    )
    target = ROOT / name
    target.write_text(json.dumps(rows), encoding="utf-8")
    size = target.stat().st_size / 1_000_000
    print(f"\n{len(rows):,} bars → {name} ({size:.1f} MB)")
    if arguments.verify:
        verify(rows, arguments.seconds, arguments.symbol, arguments.sample)


asyncio.run(main())
