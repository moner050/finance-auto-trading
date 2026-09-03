"""Answer §13.1's `B1` from Binance's own published trade history.

`B1` is "최선 + Big Trades 회피" and it is the one rung of the ladder never
run, because the Big Trade marker needs individual trade prints and the tape
this system collects only starts 2026-09-01. That was recorded as a wall in
the audit's F15.5 and the plan's §26, and it is not one: Binance publishes
the same aggregate trades at data.binance.vision, monthly, back years.

What this does not do is store a billion trades. It streams each month once,
keeps a rolling thirty-minute buffer - the window `assembly.py` uses - and at
each entry the best configuration took, computes the order-flow facts and
asks `blocking_big_trade_ahead` the same question production asks. What is
kept is one small answer per entry.

The archive is read, not the API. No rate limit is at stake and the live
loop's weight budget is untouched, which is the mistake that ended a Shadow
session on 2026-09-03.

    python scripts/backfill-big-trades.py --entries build/b1-entries.json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import zipfile
from collections import deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autotrader.domain.enums import Side
from autotrader.strategies.david_v6.order_flow import (
    BigTradesUnmeasured,
    OrderFlowThresholds,
    TradePrint,
    aggregate_order_flow,
    blocking_big_trade_ahead,
    thirty_second_atr,
)

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = "https://data.binance.vision/data/futures/um/monthly/aggTrades"
# `assembly.py:100`. The same window, or this measures a different question.
WINDOW = timedelta(minutes=30)
TICK_SIZE = Decimal("0.10")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def moment(milliseconds: int) -> datetime:
    return EPOCH + timedelta(milliseconds=milliseconds)


def download(client: httpx.Client, symbol: str, month: str, target: Path) -> None:
    url = f"{ARCHIVE}/{symbol}/{symbol}-aggTrades-{month}.zip"
    with client.stream("GET", url) as response:
        response.raise_for_status()
        size = int(response.headers.get("content-length", 0))
        written = 0
        with target.open("wb") as handle:
            for chunk in response.iter_bytes(1 << 20):
                handle.write(chunk)
                written += len(chunk)
        if size and written != size:
            raise SystemExit(f"{month}: {written} of {size} bytes")


def rows(archive: Path):
    """Every trade in the month, in file order, which is time order."""
    with zipfile.ZipFile(archive) as bundle:
        name = bundle.namelist()[0]
        with bundle.open(name) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8"))
            for row in reader:
                if not row or not row[0].isdigit():
                    # Newer archives carry a header line.
                    continue
                yield row


def answer(
    buffer: deque[TradePrint], at: datetime, side: Side, entry: Decimal
) -> dict[str, object]:
    """What production would have said about this entry, from this window."""
    window = tuple(trade for trade in buffer if at - WINDOW <= trade.occurred_at < at)
    if not window:
        return {"verdict": "NO_TRADES", "events": 0}
    atr = thirty_second_atr(window, window_start=at - WINDOW, window_end=at)
    thresholds = OrderFlowThresholds(
        tick_size=TICK_SIZE,
        # `_big_trades` does not read the ATR; it is required by the value
        # object and computed from the same window rather than invented.
        atr_30s=atr if atr and atr > 0 else Decimal("0.1"),
    )
    facts = aggregate_order_flow(
        window, window_start=at - WINDOW, window_end=at, thresholds=thresholds
    )
    try:
        blocked = blocking_big_trade_ahead(facts, side=side, reference_price=entry)
    except BigTradesUnmeasured:
        return {"verdict": "UNMEASURED", "events": len(window)}
    return {
        "verdict": "BLOCKED" if blocked else "CLEAR",
        "events": len(window),
        "markers": len(facts.big_trades or ()),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entries", default="build/b1-entries.json")
    parser.add_argument("--out", default="build/b1-markers.json")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--keep", action="store_true", help="leave the zips on disk")
    arguments = parser.parse_args()

    entries = json.loads((ROOT / arguments.entries).read_text(encoding="utf-8"))
    for entry in entries:
        entry["_at"] = datetime.fromisoformat(entry["at"])
    entries.sort(key=lambda item: item["_at"])
    months = sorted({entry["at"][:7] for entry in entries})
    print(f"진입 {len(entries):,}건, 월 {len(months)}개: {months[0]} ~ {months[-1]}")

    # Only the half hours an entry actually needs. Building a TradePrint for
    # every row of two years means about a billion validated value objects and
    # hours of it; the windows cover roughly a seventh of the time, and a row
    # outside all of them costs one integer parse.
    spans: list[list[int]] = []
    for entry in entries:
        start = int((entry["_at"] - WINDOW - EPOCH) / timedelta(milliseconds=1))
        end = int((entry["_at"] - EPOCH) / timedelta(milliseconds=1))
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    covered = sum(end - start for start, end in spans) / 1000 / 86400
    print(f"필요한 창 {len(spans):,}개, 합계 {covered:.0f}일")

    scratch = ROOT / "build" / "aggtrades"
    scratch.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    # Carried across months: an entry early in a month needs the last half
    # hour of the one before it.
    buffer: deque[TradePrint] = deque()
    pending = deque(entries)
    cursor = 0

    with httpx.Client(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
        for month in months:
            archive = scratch / f"{arguments.symbol}-aggTrades-{month}.zip"
            if not archive.exists():
                print(f"{month} 내려받는 중...", flush=True)
                download(client, arguments.symbol, month, archive)
            size = archive.stat().st_size / 1_000_000
            seen = 0
            kept = 0
            for row in rows(archive):
                seen += 1
                stamp = int(row[5])
                while cursor < len(spans) and stamp > spans[cursor][1]:
                    cursor += 1
                if cursor >= len(spans) or stamp < spans[cursor][0]:
                    continue
                kept += 1
                at = moment(stamp)
                buffer.append(
                    TradePrint(
                        provider_trade_id=row[0],
                        occurred_at=at,
                        price=Decimal(row[1]),
                        quantity=Decimal(row[2]),
                        buyer_maker=row[6].strip().lower() == "true",
                    )
                )
                while buffer and buffer[0].occurred_at < at - WINDOW:
                    buffer.popleft()
                while pending and pending[0]["_at"] <= at:
                    entry = pending.popleft()
                    verdict = answer(
                        buffer,
                        entry["_at"],
                        Side(entry["side"]),
                        Decimal(entry["entry"]),
                    )
                    results.append(
                        {
                            "at": entry["at"],
                            "side": entry["side"],
                            "r_result": entry["r_result"],
                            "r_target": entry["r_target"],
                            **verdict,
                        }
                    )
            print(
                f"  {month}  {size:.0f} MB  체결 {seen:,}  창 안 {kept:,}  "
                f"판정 누계 {len(results):,}",
                flush=True,
            )
            if not arguments.keep:
                archive.unlink()

    (ROOT / arguments.out).write_text(json.dumps(results), encoding="utf-8")
    print(f"\n{len(results):,}건 → {arguments.out}")


main()
