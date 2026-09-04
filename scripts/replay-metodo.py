"""§13.1's `T1`: the Método Trullás on daily bars, beside HLIT.

§2 is a different engine from the one the rest of this harness replays.
"항상 일봉. 절대 인트라데이가 아니다" - it holds for days on a moving-average
cross, where HLIT holds for hours against a structural stop. §13.1 asks what
running both gives, and calls it the portfolio effect.

Decisions this makes, and they are choices rather than the document's:

- **§2.1's steps [1] to [3] are dropped.** Strong country, strong sector and
  strong stock rank names against each other, and this is one perpetual.
  What survives is [4], the SMA and MACD rules of §2.2, which is the same
  reading H3 took of the same section.
- **Slope is against the previous bar.** §2.2 says the 200 must rise and the
  70 must rise without saying over what distance. The shortest honest
  reading, and the one §21's veto already uses here.
- **The exit is the opposite cross, and there is no stop.** §2.2 gives
  `sell_signal = cross_down(SMA_FAST, SMA_MID)` and nothing else. §20.1's
  structural stop belongs to the intraday entry; importing it would be
  inventing a rule this engine does not have.
- **Scored in return per trade, not in R.** Without a stop there is no risk
  unit to divide by. That is also what makes the portfolio question
  answerable: two engines can be compared in return and cannot be compared
  in each other's R.

    python scripts/replay-metodo.py --days 2190
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import statistics
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import Side

ROOT = Path(__file__).resolve().parents[1]
# §13.2 fixes these as `fixed_by_evidence`; they are not swept.
SMA_FAST, SMA_MID, SMA_SLOW = 6, 70, 200
# 288 five-minute bars is one day.
DAY = 288


def daily(bars: list[CompletedOhlcvBar]) -> list[CompletedOhlcvBar]:
    """Whole days only; a partial one at the end is dropped."""
    out: list[CompletedOhlcvBar] = []
    for start in range(0, len(bars) - DAY + 1, DAY):
        chunk = bars[start : start + DAY]
        out.append(
            CompletedOhlcvBar(
                timestamp=chunk[0].timestamp,
                open=chunk[0].open,
                high=max(bar.high for bar in chunk),
                low=min(bar.low for bar in chunk),
                close=chunk[-1].close,
                volume=sum((bar.volume for bar in chunk), Decimal(0)),
            )
        )
    return out


def averages(closes: list[Decimal], period: int) -> list[Decimal | None]:
    out: list[Decimal | None] = []
    running = Decimal(0)
    for index, value in enumerate(closes):
        running += value
        if index >= period:
            running -= closes[index - period]
        out.append(running / period if index >= period - 1 else None)
    return out


def replay(days: list[CompletedOhlcvBar]) -> list[dict[str, object]]:
    closes = [bar.close for bar in days]
    fast = averages(closes, SMA_FAST)
    mid = averages(closes, SMA_MID)
    slow = averages(closes, SMA_SLOW)

    trades: list[dict[str, object]] = []
    position: dict[str, object] | None = None
    for index in range(1, len(days)):
        if any(
            series[index] is None or series[index - 1] is None
            for series in (fast, mid, slow)
        ):
            continue
        now_fast, now_mid, now_slow = fast[index], mid[index], slow[index]
        was_fast, was_mid, was_slow = fast[index - 1], mid[index - 1], slow[index - 1]
        assert now_fast and now_mid and now_slow and was_fast and was_mid and was_slow
        price = days[index].close

        up = now_slow > was_slow and now_mid > now_slow and now_mid > was_mid
        down = now_slow < was_slow and now_mid < now_slow and now_mid < was_mid
        crossed_up = was_fast <= was_mid and now_fast > now_mid
        crossed_down = was_fast >= was_mid and now_fast < now_mid

        if position is not None:
            side = position["side"]
            if (side is Side.BUY and crossed_down) or (
                side is Side.SELL and crossed_up
            ):
                entry = Decimal(str(position["entry"]))
                move = (price - entry) if side is Side.BUY else (entry - price)
                trades.append(
                    {
                        "opened_at": position["opened_at"],
                        "closed_at": days[index].timestamp.isoformat(),
                        "side": side.value,
                        "entry": str(entry),
                        "exit": str(price),
                        "return": float(move / entry),
                        "days_held": index - int(position["index"]),
                    }
                )
                position = None

        if position is None:
            if up and crossed_up:
                side = Side.BUY
            elif down and crossed_down:
                side = Side.SELL
            else:
                continue
            position = {
                "side": side,
                "entry": price,
                "opened_at": days[index].timestamp.isoformat(),
                "index": index,
            }
    return trades


def report(trades: list[dict[str, object]], days: int) -> None:
    if not trades:
        print("거래 없음")
        return
    returns = [float(trade["return"]) for trade in trades]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    mean = statistics.fmean(returns)
    error = statistics.stdev(returns) / math.sqrt(len(returns))
    gains = sum(wins)
    pains = -sum(losses)
    compounded = 1.0
    for value in returns:
        compounded *= 1 + value

    print()
    print("=" * 58)
    print(f"거래 {len(trades)}  승 {len(wins)}  패 {len(losses)}")
    print(f"승률                           {len(wins) / len(trades) * 100:>8.1f}%")
    print(f"평균 수익률                    {mean * 100:>8.3f}%")
    print(f"  표준오차                     {error * 100:>8.3f}%")
    print(f"  t (0 대비)                   {mean / error:>8.2f}")
    average_win = statistics.fmean(wins) * 100 if wins else 0.0
    average_loss = statistics.fmean(losses) * 100 if losses else 0.0
    factor = gains / pains if pains else float("inf")
    print(f"평균 승                        {average_win:>8.2f}%")
    print(f"평균 패                        {average_loss:>8.2f}%")
    print(f"Profit factor                  {factor:>8.2f}")
    print(f"누적 (복리)                    {(compounded - 1) * 100:>8.1f}%")
    print(
        f"평균 보유일                    "
        f"{statistics.fmean(float(t['days_held']) for t in trades):>8.1f}일"
    )
    print(f"기간                           {days:>8}일")
    print("=" * 58)
    years = days / 365.25
    if years > 0 and compounded > 0:
        annual = (compounded ** (1 / years) - 1) * 100
        print(f"연환산 (거래만)                {annual:>8.1f}%")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=2190)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--out", default="build/metodo-trades.json")
    arguments = parser.parse_args()

    # The kline cache reader lives in the H0 harness; importing it keeps one
    # definition of what a cached bar is.
    spec = importlib.util.spec_from_file_location(
        "h0", ROOT / "scripts" / "replay-h0.py"
    )
    assert spec is not None and spec.loader is not None
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)

    cache = ROOT / "build" / f"klines-{arguments.symbol}-{arguments.days}d.json"
    if not cache.exists():
        raise SystemExit(f"no cache at {cache}")
    bars = list(await harness.fetch(arguments.symbol, arguments.days, cache))
    days = daily(bars)
    print(f"{len(bars):,} bars → {len(days):,} 일봉")
    print(f"{days[0].timestamp:%Y-%m-%d} → {days[-1].timestamp:%Y-%m-%d}")
    print(f"SMA {SMA_FAST}/{SMA_MID}/{SMA_SLOW}, 최초 신호 가능일 {SMA_SLOW}")

    trades = replay(days)
    (ROOT / arguments.out).write_text(json.dumps(trades, indent=1), encoding="utf-8")
    report(trades, len(days))


asyncio.run(main())
