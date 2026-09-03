"""§13.1's ladder, one rung at a time, scored in R.

    H0 = 정규 다이버전스 + 66% 목표
    H1 = H0 + 소진 확인(거래량 감소 연쇄)

`--gate h0` is the core alone: no exhaustion, no zones, no higher-timeframe
veto, no order flow. The document puts it first and says why - "여기서 기대값이
안 나오면 나머지를 붙여도 안 나온다".

`--gate h1` adds §13.1's exhaustion, which it names as the volume-decrease
chain. The zone requirement is H2's, not H1's, so the chain is checked here
without it - which is why this cannot simply call `evaluate_exhaustion`, whose
sequence is zone-gated by construction.

What this is not: a backtest of the system. It is one rung of §13.1's ladder,
and the only question it answers is whether the core has expectancy above zero.

Decisions this makes, and they are choices rather than the document's:

- **Entry** at the close of the bar that confirms the divergence. H0 has no
  zone to wait at, so there is nothing else to wait for.
- **Stop** at `invalidation_price` pushed out to a real distance. That field
  is the anchor the retracement is measured from - an invalidation *level*,
  not a stop price. Entry is the close of the bar that just made that anchor,
  so the two are a few dollars apart and every wick through the low is a loss;
  the first pilot put the average target 27R away, which is the shape of a
  stop that is not a stop. §1660 wants the structural low plus an adaptive
  buffer and `risk/v6.py` clamps the distance to 0.40-1.50 ATR, so that clamp
  is applied here.
- **Target** at `fib_66`, which is §3's target and `fixed_by_evidence`.
- **A bar that touches both counts as a loss.** Five-minute bars do not say
  which came first, and taking the favourable one is how a backtest flatters
  itself (§13.3).
- **One position at a time.** The strategy holds one; letting setups overlap
  would count the same market twice.
- **Unresolved after `HORIZON` bars is a scratch**, not a win and not a loss.
  §1828 asks for the scratch rate separately for exactly this reason.

Look-ahead: pivots are taken with `right=0`, so a pivot is confirmed by the
bar that makes it and no later bar is consulted. Every evaluation uses bars up
to and including the decision bar.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autotrader.apps.trader.risk_context import average_true_range
from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import Side
from autotrader.risk.v6 import (
    STOP_DISTANCE_MAXIMUM_ATR,
    STOP_DISTANCE_MINIMUM_ATR,
)
from autotrader.strategies.david_v6.direction import (
    aligned_macd_histogram,
)
from autotrader.strategies.david_v6.hlit import build_hlit_setups
from autotrader.strategies.david_v6.pivots import (
    Pivot,
    PivotConfig,
    PivotKind,
    confirmed_pivots,
    evaluate_divergence,
)

KLINES = "https://fapi.binance.com/fapi/v1/klines"
STEP = timedelta(minutes=5)
# Enough for MACD to have warmed up and for the last two pivots of each kind to
# be inside it. The rule reads `selected[-2:]`, so a longer window would give
# the same answer more slowly.
WINDOW = 600
# A day. The strategy is intraday and flattens before the close; without a
# session rule in H0, something has to bound the wait.
HORIZON = 288


async def fetch(symbol: str, days: int, cache: Path) -> tuple[CompletedOhlcvBar, ...]:
    if cache.exists():
        rows = json.loads(cache.read_text(encoding="utf-8"))
        print(f"cache {cache.name}: {len(rows)} bars", flush=True)
    else:
        end = datetime.now(UTC).replace(second=0, microsecond=0)
        start = end - timedelta(days=days)
        rows, cursor = [], int(start.timestamp() * 1000)
        async with httpx.AsyncClient(timeout=60) as client:
            while cursor < int(end.timestamp() * 1000):
                response = await client.get(
                    KLINES,
                    params={
                        "symbol": symbol,
                        "interval": "5m",
                        "startTime": cursor,
                        "limit": 1500,
                    },
                )
                response.raise_for_status()
                page = response.json()
                if not page:
                    break
                rows.extend(
                    [[row[0], row[1], row[2], row[3], row[4], row[5]] for row in page]
                )
                cursor = page[-1][0] + 1
                if len(rows) % 30000 < 1500:
                    print(f"  fetched {len(rows)}", flush=True)
        cache.write_text(json.dumps(rows), encoding="utf-8")
        print(f"fetched {len(rows)} bars into {cache.name}", flush=True)
    return tuple(
        CompletedOhlcvBar(
            timestamp=datetime.fromtimestamp(row[0] / 1000, UTC),
            open=Decimal(row[1]),
            high=Decimal(row[2]),
            low=Decimal(row[3]),
            close=Decimal(row[4]),
            volume=Decimal(row[5]),
        )
        for row in rows
    )


def evaluation_points(bars: Sequence[CompletedOhlcvBar]) -> tuple[int, ...]:
    """Bars where a new pivot was confirmed.

    Between confirmations the last two pivots of each kind are unchanged, so
    the divergence verdict cannot change either. Evaluating every bar would ask
    the same question a hundred times over.
    """
    return tuple(
        sorted(
            {
                pivot.confirmation_index
                for pivot in confirmed_pivots(bars, PivotConfig())
            }
        )
    )


def exhaustion_legs(
    bars: Sequence[CompletedOhlcvBar], pivots: Sequence[Pivot], side: Side
) -> int:
    """How many legs the current volume-decrease chain has.

    Mirrors `exhaustion._sequence` with the zone test left out: §13.1 puts the
    chain in H1 and the zone requirement in H2, so folding them together would
    make H1 unmeasurable. Price extends the extreme and volume falls, and
    either failure resets the chain - the document's "새 저점이 나오는데
    거래량이 계단처럼 줄어드는" read literally.
    """
    kind = PivotKind.LOW if side is Side.BUY else PivotKind.HIGH
    selected = sorted(
        (pivot for pivot in pivots if pivot.confirmed and pivot.kind is kind),
        key=lambda pivot: pivot.index,
    )
    if len(selected) < 2:
        return 0
    legs = 1
    for previous, current in pairwise(selected):
        extends = (
            current.price < previous.price
            if kind is PivotKind.LOW
            else current.price > previous.price
        )
        quieter = bars[current.index].volume < bars[previous.index].volume
        legs = legs + 1 if extends and quieter else 1
    return legs


def _stop_price(
    entry: Decimal, invalidation: Decimal, side: Side, atr: Decimal
) -> Decimal:
    """The structural level, moved out to a distance the risk engine allows.

    `risk/v6.py` refuses a stop closer than 0.40 ATR and further than 1.50, and
    those two constants are imported rather than repeated so this cannot drift
    from what production would accept.
    """
    distance = entry - invalidation if side is Side.BUY else invalidation - entry
    floor = STOP_DISTANCE_MINIMUM_ATR * atr
    ceiling = STOP_DISTANCE_MAXIMUM_ATR * atr
    distance = min(max(distance, floor), ceiling)
    return entry - distance if side is Side.BUY else entry + distance


def resolve(
    bars: Sequence[CompletedOhlcvBar],
    opened: int,
    side: Side,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
) -> tuple[str, int, Decimal, Decimal]:
    """Walk forward until the stop or the target is touched."""
    best = worst = Decimal(0)
    for index in range(opened + 1, min(opened + 1 + HORIZON, len(bars))):
        bar = bars[index]
        if side is Side.BUY:
            best = max(best, bar.high - entry)
            worst = min(worst, bar.low - entry)
            if bar.low <= stop:
                return "loss", index, best, worst
            if bar.high >= target:
                return "win", index, best, worst
        else:
            best = max(best, entry - bar.low)
            worst = min(worst, entry - bar.high)
            if bar.high >= stop:
                return "loss", index, best, worst
            if bar.low <= target:
                return "win", index, best, worst
    return "scratch", min(opened + HORIZON, len(bars) - 1), best, worst


def replay(
    bars: tuple[CompletedOhlcvBar, ...], *, gate: str, min_legs: int
) -> list[dict[str, object]]:
    trades: list[dict[str, object]] = []
    points = evaluation_points(bars)
    print(f"{len(bars)} bars, {len(points)} evaluation points", flush=True)
    busy_until = -1
    seen: set[tuple[str, str]] = set()
    for done, cut in enumerate(points):
        if cut < WINDOW or cut <= busy_until:
            continue
        if done % 2000 == 0:
            print(f"  {done}/{len(points)}  trades {len(trades)}", flush=True)
        window = bars[cut - WINDOW : cut + 1]
        # The pivot indices in a divergence are relative to the MACD-aligned
        # bars, not to the window it was computed from - `assembly.py` hands
        # `macd_bars` to `build_hlit_setups` for that reason. Passing the
        # untrimmed window silently looks for the anchor in the wrong place and
        # every setup comes back None.
        aligned = aligned_macd_histogram(window)
        if aligned is None:
            continue
        aligned_bars, histogram = aligned
        divergence = evaluate_divergence(aligned_bars, histogram)
        if not divergence.regular:
            continue
        facts = build_hlit_setups(aligned_bars, divergence)
        aligned_pivots = (
            confirmed_pivots(aligned_bars, PivotConfig()) if gate == "h1" else ()
        )
        for setup in (facts.bullish, facts.bearish):
            if setup is None:
                continue
            if gate == "h1" and (
                exhaustion_legs(aligned_bars, aligned_pivots, setup.direction)
                < min_legs
            ):
                continue
            # One setup per divergence pair; the same pair persists for many
            # bars and would otherwise be opened again and again. Keyed by the
            # anchor bar's timestamp because the index is relative to a window
            # that moves.
            key = (
                aligned_bars[setup.second_pivot_index].timestamp.isoformat(),
                setup.direction.value,
            )
            if key in seen:
                continue
            seen.add(key)
            entry = bars[cut].close
            atr = average_true_range(window)
            if atr is None or atr <= 0:
                continue
            stop = _stop_price(entry, setup.invalidation_price, setup.direction, atr)
            target = setup.target_price
            risk = entry - stop if setup.direction is Side.BUY else stop - entry
            reward = target - entry if setup.direction is Side.BUY else entry - target
            if risk <= 0 or reward <= 0:
                continue
            outcome, closed, best, worst = resolve(
                bars, cut, setup.direction, entry, stop, target
            )
            trades.append(
                {
                    "opened_at": bars[cut].timestamp.isoformat(),
                    "side": setup.direction.value,
                    "outcome": outcome,
                    "r_target": float(reward / risk),
                    "r_result": {"win": float(reward / risk), "loss": -1.0}.get(
                        outcome,
                        float((bars[closed].close - entry) / risk)
                        if setup.direction is Side.BUY
                        else float((entry - bars[closed].close) / risk),
                    ),
                    "bars_held": closed - cut,
                    "mfe_r": float(best / risk),
                    "mae_r": float(worst / risk),
                }
            )
            busy_until = closed
            break
    return trades


def report(trades: list[dict[str, object]]) -> None:
    if not trades:
        print("\n거래 없음")
        return
    wins = [t for t in trades if t["outcome"] == "win"]
    losses = [t for t in trades if t["outcome"] == "loss"]
    scratches = [t for t in trades if t["outcome"] == "scratch"]
    results = [float(t["r_result"]) for t in trades]
    gains = sum(r for r in results if r > 0)
    pains = -sum(r for r in results if r < 0)
    decided = len(wins) + len(losses)
    factor = gains / pains if pains else float("inf")
    expectancy = sum(results) / len(results)

    def average(rows: list[dict[str, object]], field: str) -> float:
        return sum(float(row[field]) for row in rows) / len(rows) if rows else 0.0

    print("\n" + "=" * 58)
    print(
        f"거래 {len(trades)}  승 {len(wins)}  패 {len(losses)}  무승부 {len(scratches)}"
    )
    if decided:
        share = len(wins) / decided * 100
        print(f"Win rate excluding scratches   {share:>8.1f}%")
    print(f"Scratch rate                   {len(scratches) / len(trades) * 100:>8.1f}%")
    print(f"Loss rate                      {len(losses) / len(trades) * 100:>8.1f}%")
    print(f"Average win                    {average(wins, 'r_result'):>8.2f} R")
    print(f"Average loss                   {average(losses, 'r_result'):>8.2f} R")
    print(f"Profit factor                  {factor:>8.2f}")
    print(f"Expectancy                     {expectancy:>8.3f} R")
    print(
        f"MFE / MAE (평균)               {average(trades, 'mfe_r'):>8.2f}"
        f" / {average(trades, 'mae_r'):.2f} R"
    )
    print(f"평균 목표 거리                 {average(trades, 'r_target'):>8.2f} R")
    print("=" * 58)
    print("\n§22.9 core_hlit 기준:")
    passed = "통과"
    failed = "미달"
    print(
        f"  expectancy >= 0.15 R    {expectancy:>8.3f}  "
        f"{passed if expectancy >= 0.15 else failed}"
    )
    print(
        f"  profit factor >= 1.15   {factor:>8.2f}  "
        f"{passed if factor >= 1.15 else failed}"
    )
    print(
        f"  setups >= 50            {len(trades):>8}  "
        f"{passed if len(trades) >= 50 else failed}"
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--out", default="build/h0-trades.json")
    parser.add_argument("--gate", choices=("h0", "h1"), default="h0")
    parser.add_argument("--min-legs", type=int, default=3)
    arguments = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    cache = root / "build" / f"klines-{arguments.symbol}-{arguments.days}d.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    bars = await fetch(arguments.symbol, arguments.days, cache)
    print(f"{bars[0].timestamp} → {bars[-1].timestamp}", flush=True)
    print(
        f"gate {arguments.gate}"
        + (f", min_legs {arguments.min_legs}" if arguments.gate == "h1" else ""),
        flush=True,
    )
    trades = replay(bars, gate=arguments.gate, min_legs=arguments.min_legs)
    (root / arguments.out).write_text(json.dumps(trades, indent=1), encoding="utf-8")
    report(trades)


if __name__ == "__main__":
    asyncio.run(main())
