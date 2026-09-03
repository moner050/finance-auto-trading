"""Sweep §22.5's marker cap over the recorded windows, without re-downloading.

`_big_trades` keeps the largest `MAXIMUM_BIG_TRADE_MARKERS` events by
notional, so the top N of a recorded twenty is exactly what a cap of N would
have kept. The backfill records those twenty with their notionals, which
turns F12's question - is a cap of 20 the session grid applied to a
thirty-minute window - into arithmetic over a nine-megabyte file.

The blocking condition is re-implemented here rather than called, because
rebuilding a `BigTradeCluster` needs fields the record does not carry. That
is a risk, so it is checked: at cap 20 this must reproduce the verdict the
backfill wrote for all 5,061 entries, and the sweep refuses to run if it
does not.

    python scripts/sweep-big-trade-cap.py
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# §22.5's grid is one to three events per liquidity session; F12 found the
# code's 20 is that grid read against a thirty-minute window. The sweep spans
# both readings and what lies between.
GRID = (1, 2, 3, 5, 10, 20)


def blocked(
    side: str, clusters: list[dict[str, str]], price: Decimal, cap: int
) -> bool:
    """`blocking_big_trade_ahead`, over the largest `cap` markers.

    Ahead means above the reference for a long and below it for a short, and
    opposing means the aggressor pushed against the traded direction.
    """
    ranked = sorted(clusters, key=lambda item: Decimal(item["notional"]), reverse=True)
    opposing = "SELL" if side == "BUY" else "BUY"
    return any(
        cluster["side"] == opposing
        and (
            Decimal(cluster["high"]) >= price
            if side == "BUY"
            else Decimal(cluster["low"]) <= price
        )
        for cluster in ranked[:cap]
    )


def describe(label: str, values: list[float]) -> tuple[float, float] | None:
    if len(values) < 2:
        print(f"| {label} | {len(values)} | — | — | — | — |")
        return None
    mean = statistics.fmean(values)
    error = statistics.stdev(values) / math.sqrt(len(values))
    print(
        f"| {label} | {len(values):,} | {mean:+.4f} | {error:.4f} | "
        f"{mean / error:+.2f} | {(mean - 0.15) / error:+.2f} |"
    )
    return mean, error


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clusters", default="build/b1-clusters.json")
    parser.add_argument("--entries", default="build/b1-entries.json")
    arguments = parser.parse_args()

    verdicts = json.loads((ROOT / arguments.clusters).read_text(encoding="utf-8"))
    prices = {
        entry["at"]: Decimal(entry["entry"])
        for entry in json.loads((ROOT / arguments.entries).read_text(encoding="utf-8"))
    }
    usable = [row for row in verdicts if row["verdict"] in ("BLOCKED", "CLEAR")]
    print(f"판정 {len(verdicts):,}건 중 마커가 기록된 것 {len(usable):,}")

    disagreements = sum(
        1
        for row in usable
        if blocked(row["side"], row["clusters"], prices[row["at"]], 20)
        != (row["verdict"] == "BLOCKED")
    )
    if disagreements:
        raise SystemExit(
            f"상한 20에서 기록과 {disagreements}건 불일치 — 조건 재구현이 틀렸다"
        )
    print("상한 20에서 기록된 판정을 5,061건 모두 재현 — 재구현 검증됨")

    base = [row["r_result"] for row in usable]
    print()
    print("| 상한 | 통과 | 기대값 R | se | t(0) | t(0.15) |")
    print("|---|---:|---:|---:|---:|---:|")
    describe("차단 없음", base)
    for cap in GRID:
        clear = [
            row["r_result"]
            for row in usable
            if not blocked(row["side"], row["clusters"], prices[row["at"]], cap)
        ]
        describe(f"상한 {cap}", clear)

    print()
    print("| 상한 | 차단율 | 통과 | `<1R` 비중 |")
    print("|---|---:|---:|---:|")
    for cap in GRID:
        survivors = [
            row
            for row in usable
            if not blocked(row["side"], row["clusters"], prices[row["at"]], cap)
        ]
        share = 1 - len(survivors) / len(usable)
        sub = (
            sum(1 for row in survivors if row["r_target"] < 1) / len(survivors)
            if survivors
            else 0.0
        )
        print(f"| {cap} | {share:.1%} | {len(survivors):,} | {sub:.1%} |")


main()
