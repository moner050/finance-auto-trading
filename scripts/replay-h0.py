"""§13.1's H0: the HLIT core alone, scored in R.

    H0 = 정규 다이버전스 + 66% 목표

No exhaustion, no zones, no higher-timeframe veto, no order flow. The document
puts this first and says why: "여기서 기대값이 안 나오면 나머지를 붙여도 안
나온다".

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
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar  # noqa: E402
from autotrader.apps.trader.risk_context import average_true_range  # noqa: E402
from autotrader.domain.enums import Side  # noqa: E402
from autotrader.strategies.david_v6.direction import (  # noqa: E402
    aligned_macd_histogram,
)
from autotrader.strategies.david_v6.hlit import build_hlit_setups  # noqa: E402
from autotrader.risk.v6 import (  # noqa: E402
    STOP_DISTANCE_MAXIMUM_ATR,
    STOP_DISTANCE_MINIMUM_ATR,
)
from autotrader.strategies.david_v6.pivots import (  # noqa: E402
    PivotConfig,
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
                rows.extend([[row[0], row[1], row[2], row[3], row[4], row[5]] for row in page])
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
        sorted({pivot.confirmation_index for pivot in confirmed_pivots(bars, PivotConfig())})
    )



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


def replay(bars: tuple[CompletedOhlcvBar, ...]) -> list[dict[str, object]]:
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
        for setup in (facts.bullish, facts.bearish):
            if setup is None:
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
                        outcome, float((bars[closed].close - entry) / risk)
                        if setup.direction is Side.BUY
                        else float((entry - bars[closed].close) / risk)
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
    print("\n" + "=" * 58)
    print(f"거래 {len(trades)}  승 {len(wins)}  패 {len(losses)}  무승부 {len(scratches)}")
    if decided:
        print(f"Win rate excluding scratches   {len(wins)/decided*100:>8.1f}%")
    print(f"Scratch rate                   {len(scratches)/len(trades)*100:>8.1f}%")
    print(f"Loss rate                      {len(losses)/len(trades)*100:>8.1f}%")
    if wins:
        print(f"Average win                    {sum(float(t['r_result']) for t in wins)/len(wins):>8.2f} R")
    if losses:
        print(f"Average loss                   {sum(float(t['r_result']) for t in losses)/len(losses):>8.2f} R")
    print(f"Profit factor                  {gains/pains if pains else float('inf'):>8.2f}")
    print(f"Expectancy                     {sum(results)/len(results):>8.3f} R")
    print(f"MFE / MAE (평균)               {sum(float(t['mfe_r']) for t in trades)/len(trades):>8.2f}"
          f" / {sum(float(t['mae_r']) for t in trades)/len(trades):.2f} R")
    print(f"평균 목표 거리                 {sum(float(t['r_target']) for t in trades)/len(trades):>8.2f} R")
    print("=" * 58)
    print("\n§22.9 core_hlit 기준:")
    expectancy = sum(results) / len(results)
    factor = gains / pains if pains else float("inf")
    print(f"  expectancy >= 0.15 R    {expectancy:>8.3f}  {'통과' if expectancy >= 0.15 else '미달'}")
    print(f"  profit factor >= 1.15   {factor:>8.2f}  {'통과' if factor >= 1.15 else '미달'}")
    print(f"  setups >= 50            {len(trades):>8}  {'통과' if len(trades) >= 50 else '미달'}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--out", default="build/h0-trades.json")
    arguments = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    cache = root / "build" / f"klines-{arguments.symbol}-{arguments.days}d.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    bars = await fetch(arguments.symbol, arguments.days, cache)
    print(f"{bars[0].timestamp} → {bars[-1].timestamp}", flush=True)
    trades = replay(bars)
    (root / arguments.out).write_text(json.dumps(trades, indent=1), encoding="utf-8")
    report(trades)


if __name__ == "__main__":
    asyncio.run(main())
