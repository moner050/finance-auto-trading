"""§13.1's ladder, one rung at a time, scored in R.

    H0 = 정규 다이버전스 + 66% 목표
    H1 = H0 + 소진 확인(거래량 감소 연쇄)
    H2 = H1 + 사전 존 겹침 요건

`--gate h0` is the core alone: no exhaustion, no zones, no higher-timeframe
veto, no order flow. The document puts it first and says why - "여기서 기대값이
안 나오면 나머지를 붙여도 안 나온다".

`--gate h1` adds §13.1's exhaustion, which it names as the volume-decrease
chain. The zone requirement is H2's, not H1's, so the chain is checked here
without it - which is why this cannot simply call `evaluate_exhaustion`, whose
sequence is zone-gated by construction.

`--gate h2` puts the zone back and therefore does call `evaluate_exhaustion`,
the production function, which is the whole difference between the two rungs.

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
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
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
from autotrader.strategies.david_v6.exhaustion import evaluate_exhaustion
from autotrader.strategies.david_v6.hlit import HlitSetup, build_hlit_setups
from autotrader.strategies.david_v6.pivots import (
    DivergenceFacts,
    DivergenceKind,
    Pivot,
    PivotConfig,
    PivotKind,
    confirmed_pivots,
    evaluate_divergence,
)
from autotrader.strategies.david_v6.zones import (
    ZONE_HISTORY,
    HlitZone,
    ZoneConfig,
    build_hlit_zones,
)

KLINES = "https://fapi.binance.com/fapi/v1/klines"
STEP = timedelta(minutes=5)
# Zones want eleven days where the divergence wants six hundred bars, and
# building them costs 76ms. Rebuilt once an hour rather than at every
# evaluation point: they come from ten distinct dates and barely move inside
# an hour. An approximation, and stated rather than hidden.
ZONE_WINDOW = int(ZONE_HISTORY / timedelta(minutes=5))
ZONE_REFRESH = 12
# Enough for MACD to have warmed up and for the last two pivots of each kind to
# be inside it. The rule reads `selected[-2:]`, so a longer window would give
# the same answer more slowly.
WINDOW = 600
# A day. The strategy is intraday and flattens before the close; without a
# session rule in H0, something has to bound the wait.
HORIZON = 288
# Enough execution-scale bars for the pivots the chain is built from. Counted
# in bars rather than in time, because that is what `PivotConfig` counts.
EXECUTION_WINDOW = 240

# §33.7's X2: where `_execution_entry` gave up, one count per setup.
ENTRY_CENSUS: dict[str, int] = defaultdict(int)
# And how far the chain got before the search ran out: the best leg count
# any bar in the window reached. A chain that never reaches two is a
# different failure from one that reaches two and never three.
ENTRY_BEST_LEGS: dict[int, int] = defaultdict(int)
# How many bars the search got through before it ended.
ENTRY_BARS: list[int] = []
# §33.9. Where the setup already stands at the five-minute bar that made it,
# before any execution-scale search: the same two tests, at detection.
SETUP_AT_DETECTION: dict[str, int] = defaultdict(int)


@dataclass(frozen=True, slots=True)
class ExecutionScale:
    """One way of reading §13.2's `execution_scale: [1m, 30s, 5s]`.

    `entry_window` is one hour in bars of this scale. §4.2 drops to the
    execution scale straight after the five-minute signal and enters there,
    so the search is given the same hour whichever scale it runs at - sixty
    one-minute bars or a hundred and twenty thirty-second ones. Leaving it at
    sixty would have given thirty seconds half the chance to find a chain and
    called the difference a result.

    Five seconds is not here: the klines endpoint stops at one minute, and
    `scripts/aggregate-tape-bars.py` can build five-second bars from the tape
    the same way, but the tape does not yet reach back far enough to be worth
    a run. See the plan's section 26.
    """

    step: timedelta
    entry_window: int
    cache: str
    # The klines interval, where it differs from the key. "5m-chain" is a way
    # of running the search, not an interval Binance knows.
    interval: str | None = None

    @property
    def per_five_minutes(self) -> int:
        return int(STEP / self.step)


EXECUTION_SCALES = {
    "1m": ExecutionScale(
        step=timedelta(minutes=1),
        entry_window=60,
        cache="klines-{symbol}-1m-{days}d.json",
    ),
    "30s": ExecutionScale(
        step=timedelta(seconds=30),
        entry_window=120,
        # Built from the tape, which has one span rather than a day count.
        cache="klines-{symbol}-30s-tape.json",
    ),
    # §33.8's control. Not a scale the document asks for - it is five minutes,
    # which is where detection already runs - but `--execution-scale 5m` means
    # "off", so there was no way to run this search on five-minute bars and
    # compare like with like. One hour is twelve bars here.
    "5m-chain": ExecutionScale(
        step=STEP,
        entry_window=12,
        cache="klines-{symbol}-{days}d.json",
        interval="5m",
    ),
}


# A 1500-bar page costs weight 10 against an IP budget of 2400 a minute.
# Unthrottled, this loop took a live Shadow session down with a 429 - the
# backfill and the loop share one address. A hundred pages a minute leaves
# the loop most of the budget and still fetches a year of minutes in a few
# minutes.
_FETCH_INTERVAL = 0.6
_FETCH_RETRIES = 6


async def fetch(
    symbol: str, days: int, cache: Path, interval: str = "5m"
) -> tuple[CompletedOhlcvBar, ...]:
    """Cached klines at `interval`, which the cache name has to agree with.

    The interval used to be hardcoded to five minutes while the caller chose
    the file, so asking for a one-minute cache would have filled it with
    five-minute bars under a name that said otherwise.
    """
    if cache.exists():
        rows = json.loads(cache.read_text(encoding="utf-8"))
        print(f"cache {cache.name}: {len(rows)} bars", flush=True)
    else:
        end = datetime.now(UTC).replace(second=0, microsecond=0)
        start = end - timedelta(days=days)
        rows, cursor = [], int(start.timestamp() * 1000)
        print(f"fetching {interval} {symbol} into {cache.name}", flush=True)
        async with httpx.AsyncClient(timeout=60) as client:
            while cursor < int(end.timestamp() * 1000):
                page = await _fetch_page(client, symbol, interval, cursor)
                if not page:
                    break
                rows.extend(
                    [[row[0], row[1], row[2], row[3], row[4], row[5]] for row in page]
                )
                cursor = page[-1][0] + 1
                if len(rows) % 60000 < 1500:
                    print(f"  fetched {len(rows)}", flush=True)
                await asyncio.sleep(_FETCH_INTERVAL)
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


async def _fetch_page(
    client: httpx.AsyncClient, symbol: str, interval: str, cursor: int
) -> list[list[object]]:
    """One page, backing off rather than giving the address up to a ban."""
    for attempt in range(_FETCH_RETRIES):
        response = await client.get(
            KLINES,
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "limit": 1500,
            },
        )
        if response.status_code in (418, 429) or response.status_code >= 500:
            # 418 is the ban that follows ignored 429s, so it waits longest.
            delay = float(response.headers.get("Retry-After") or 2 ** (attempt + 1))
            print(f"  {response.status_code}, waiting {delay:.0f}s", flush=True)
            await asyncio.sleep(delay)
            continue
        response.raise_for_status()
        page: list[list[object]] = response.json()
        return page
    raise SystemExit(f"klines refused {_FETCH_RETRIES} times at {cursor}")


def evaluation_points(
    bars: Sequence[CompletedOhlcvBar], config: PivotConfig | None = None
) -> tuple[int, ...]:
    """Bars where a new pivot was confirmed.

    Between confirmations the last two pivots of each kind are unchanged, so
    the divergence verdict cannot change either. Evaluating every bar would ask
    the same question a hundred times over.
    """
    return tuple(
        sorted(
            {
                pivot.confirmation_index
                for pivot in confirmed_pivots(bars, config or PivotConfig())
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
    entry: Decimal,
    invalidation: Decimal,
    side: Side,
    atr: Decimal,
    floor_atr: Decimal = STOP_DISTANCE_MINIMUM_ATR,
    ceiling_atr: Decimal = STOP_DISTANCE_MAXIMUM_ATR,
) -> Decimal | None:
    """The structural level, or None where production would refuse the setup.

    This used to move the distance into 0.40-1.50 ATR, which is not what
    `risk/v6.py` does. Production places the stop at the structural
    reference and never moves it: outside that band it appends
    STOP_DISTANCE_BELOW_0_40_ATR5M or STOP_DISTANCE_ABOVE_1_50_ATR5M, and a
    blocker is a refusal - `allowed = not canonical_blockers`.

    Clamping traded what production declines, and it manufactured the result
    the earlier runs reported. Pushing a too-tight stop out to the floor
    leaves the 0.66 target where it was, so the reward falls below the risk;
    half of H0's sample sat in that sub-1R bucket and carried all of the
    loss. The constants are still imported rather than repeated, but now
    they are read the way production reads them.

    The two bounds are arguments because §13.2 lists them as free parameters -
    `stop_min_atr: [0.3, 0.4, 0.5]` and `stop_max_atr: [1.2, 1.5, 2.0]` - and
    production's 0.40 and 1.50 are one point of that grid that was never
    swept. The defaults are still production's, so a run that says nothing
    reproduces production.
    """
    distance = entry - invalidation if side is Side.BUY else invalidation - entry
    floor = floor_atr * atr
    ceiling = ceiling_atr * atr
    if not floor <= distance <= ceiling:
        return None
    return entry - distance if side is Side.BUY else entry + distance


def resolve(
    bars: Sequence[CompletedOhlcvBar],
    opened: int,
    side: Side,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    horizon: int = HORIZON,
    breakeven_at: Decimal | None = None,
    add_at: Decimal | None = None,
    atr: Decimal | None = None,
) -> tuple[str, int, Decimal, Decimal, Decimal, Decimal]:
    """Walk forward until the stop or the target is touched.

    The horizon is in bars of whatever series is passed, so a minute series
    gets five times as many for the same day.

    `breakeven_at` is §13.1's S1: once the trade is that many R in front,
    the stop moves to the entry. §20.2 puts it at "진입가보다 1.5포인트
    유리한 곳" to cover the round trip, and this harness models no fees, so
    the stop goes to the entry exactly. That makes the result an upper bound
    on what S1 is worth - a real stop sits past the entry and is touched
    more often than one placed on it.

    `add_at` is S1's neighbour P1: §22.7's add trigger, a multiple of ATR,
    after which a second unit is held and the stop moves to the weighted
    entry. Both units are the same size, so the weighted entry is the
    midpoint, and the result is scored against the risk the first unit
    started with - which is what the R in every other row here means.

    The last two returned values are the unit count and what the position
    actually made, in price. The caller used to assume a stop-out is -1R,
    which is true only while the stop is where it started: a stop moved to
    the entry or to a weighted entry exits for about nothing, and scoring
    that as a full loss made both S1 and P1 look ruinous when first run.

    Profit is measured against the weighted entry and multiplied by the
    units, because two fills at different prices make
    `(exit - weighted) * units` the P&L and `(exit - entry) * units`
    something else. With no add, weighted is the entry and the arithmetic
    is the same as before.
    """
    best = worst = Decimal(0)
    units = Decimal(1)
    weighted = entry
    added = False
    moved = False
    risk = entry - stop if side is Side.BUY else stop - entry

    def realised(price: Decimal) -> Decimal:
        move = price - weighted if side is Side.BUY else weighted - price
        return move * units

    for index in range(opened + 1, min(opened + 1 + horizon, len(bars))):
        bar = bars[index]
        if side is Side.BUY:
            best = max(best, bar.high - entry)
            worst = min(worst, bar.low - entry)
            if bar.low <= stop:
                return "loss", index, best, worst, units, realised(stop)
            if bar.high >= target:
                return "win", index, best, worst, units, realised(target)
            if not added and add_at is not None and atr is not None:
                trigger = entry + add_at * atr
                if bar.high >= trigger:
                    # Same size, so the weighted entry is the midpoint, and
                    # §22.7 puts the stop there. Total risk falls, which is
                    # what its last line requires of an add.
                    weighted = (entry + trigger) / 2
                    stop = max(stop, weighted)
                    units = Decimal(2)
                    added = True
                    moved = True
            if (
                not moved
                and breakeven_at is not None
                and risk > 0
                and bar.high - entry >= breakeven_at * risk
            ):
                stop = max(stop, entry)
                moved = True
            # The moved stop applies to the bar that moved it. A bar can
            # reach the trigger and fall back through the new stop, and
            # leaving that to the next bar is §13.3's warning exactly -
            # taking the favourable reading of an ambiguous bar.
            if moved and bar.low <= stop:
                return "loss", index, best, worst, units, realised(stop)
        else:
            best = max(best, entry - bar.low)
            worst = min(worst, entry - bar.high)
            if bar.high >= stop:
                return "loss", index, best, worst, units, realised(stop)
            if bar.low <= target:
                return "win", index, best, worst, units, realised(target)
            if not added and add_at is not None and atr is not None:
                trigger = entry - add_at * atr
                if bar.low <= trigger:
                    weighted = (entry + trigger) / 2
                    stop = min(stop, weighted)
                    units = Decimal(2)
                    added = True
                    moved = True
            if (
                not moved
                and breakeven_at is not None
                and risk > 0
                and entry - bar.low >= breakeven_at * risk
            ):
                stop = min(stop, entry)
                moved = True
            if moved and bar.high >= stop:
                return "loss", index, best, worst, units, realised(stop)
    last = min(opened + horizon, len(bars) - 1)
    return "scratch", last, best, worst, units, realised(bars[last].close)


def _setup_level(setup: HlitSetup, name: str) -> Decimal | None:
    """A retracement level off the setup itself, with no zone involved.

    Section 12.4 named what the two zone readings both missed. The entry that
    loses the money is the anchor bar's close, which sits a median 0.86 ATR
    above the bar's low while the whole distance to the target is 0.97 ATR;
    what is needed is a level that makes that gap small, and section 12.4
    says where it is - "앵커 B 근처". `nearest` aimed just under the close and
    `anchor` aimed at a zone's upper edge, usually above it. Neither could.

    These are the retracement's own levels, so they exist for every setup
    rather than only for the ones whose anchor happened to fall inside a
    marked rectangle. For a bullish setup `anchor_b` is the low the
    divergence was made at and `fib_25`/`fib_50` sit above it, so all three
    ask price to come back down before entering - which is section 4.2's
    step [2], and the close entry is not.
    """
    if name == "anchor_b":
        return setup.anchor_b
    if name == "fib25":
        return setup.fib_25
    if name == "fib50":
        return setup.fib_50
    return None


def _nearest_zone_level(
    setup: HlitSetup, zones: Sequence[HlitZone], price: Decimal
) -> Decimal | None:
    """The nearest pre-marked zone between the current price and the anchor.

    Section 10 builds these from clustered highs, lows, opens and closes with
    at least three touches - the red rectangles that are "already drawn"
    before the setup exists. A pullback entry needs one below the price for a
    long, because that is the direction price has to come back from, and
    above the invalidation, because below it the setup is finished.

    Measured: this waits a median of one bar. Price dips for a single
    five-minute candle and the entry fires, which is not the step [2] of
    section 4.2 that it was written for. Kept because it is what the
    two-year runs in section 11.4 used, and replacing a reading rather than
    measuring beside it is how a comparison stops being possible.
    """
    if setup.direction is Side.BUY:
        below = [
            zone.upper_boundary
            for zone in zones
            if setup.invalidation_price < zone.upper_boundary < price
        ]
        return max(below) if below else None
    above = [
        zone.lower_boundary
        for zone in zones
        if price < zone.lower_boundary < setup.invalidation_price
    ]
    return min(above) if above else None


def _anchor_zone_level(
    setup: HlitSetup, zones: Sequence[HlitZone], price: Decimal
) -> Decimal | None:
    """Section 4.1's own zone: the one the setup's anchor sits inside.

    "나는 그 존이 표시되어 있었지만" - that zone, one specific rectangle
    marked before the setup existed, not whichever happens to lie nearest
    below the price. For a bullish setup the anchor is min#2, the low that
    made the divergence (§3.1 STEP 1 and 3), so the zone in question is the
    one holding that low, and its upper boundary is where price coming back
    down first touches it.

    None where no zone holds the anchor. That is not a filter chosen here -
    section 4.1's entry is a return to a marked zone, and a setup whose
    anchor was never in one has no such zone to return to.
    """
    if setup.direction is Side.BUY:
        holding = [
            zone.upper_boundary
            for zone in zones
            if zone.lower_boundary <= setup.invalidation_price <= zone.upper_boundary
        ]
        if not holding:
            return None
        # Above the price means price is inside the zone already: the
        # approach has happened and there is nothing left to wait for.
        return min(max(holding), price)
    holding = [
        zone.lower_boundary
        for zone in zones
        if zone.lower_boundary <= setup.invalidation_price <= zone.upper_boundary
    ]
    if not holding:
        return None
    return max(min(holding), price)


def _execution_entry(
    bars: Sequence[CompletedOhlcvBar],
    start: int,
    setup: HlitSetup,
    min_legs: int,
    entry_window: int,
) -> tuple[int, Decimal] | None:
    """The bar where the chain completes at the execution scale, and its stop.

    Section 4.2 orders it: five-minute macro, then "1분 + 30초 분할 화면으로
    하강", then the volume divergence read there, then entry. Section 9.1's
    form C names what that buys - "5초 조기 진입(타이트 손절)". The entry is
    precise, so the stop is close, so the 66% target is far in R terms.

    Section 12.3 measured what the five-minute close costs instead: with
    d = close - low, the median d is 0.86 ATR while the whole target distance
    is 0.97 ATR. There is nothing left. On one-minute bars the same moment
    has a close much nearer its own low.

    The stop reference is section 20.1's, read literally: "롱: 최종 소진
    다리의 저점" - the low of the final exhaustion leg, not the five-minute
    anchor. It has to sit above the anchor for a long, because below it the
    setup is already finished.

    Returns the entry index and that stop reference, or None: the anchor
    breaks, the target arrives without us, or the chain never completes
    inside `ENTRY_WINDOW`.
    """
    long = setup.direction is Side.BUY
    kind = PivotKind.LOW if long else PivotKind.HIGH
    # §33.7's X2. The last reason this setup was still looking, so a setup
    # that runs out of window is attributed to what it was waiting for
    # rather than to the window.
    waiting = "NO_HISTORY"
    best_legs = 0
    for index in range(start, min(start + entry_window, len(bars))):
        bar = bars[index]
        first = " (첫 봉)" if index == start else ""
        if long:
            if bar.low <= setup.invalidation_price:
                ENTRY_CENSUS["ANCHOR_BROKEN" + first] += 1
                ENTRY_BARS.append(index - start)
                return None
            if bar.high >= setup.target_price:
                ENTRY_CENSUS["TARGET_FIRST" + first] += 1
                ENTRY_BARS.append(index - start)
                return None
        else:
            if bar.high >= setup.invalidation_price:
                ENTRY_CENSUS["ANCHOR_BROKEN" + first] += 1
                ENTRY_BARS.append(index - start)
                return None
            if bar.low <= setup.target_price:
                ENTRY_CENSUS["TARGET_FIRST" + first] += 1
                ENTRY_BARS.append(index - start)
                return None
        if index < EXECUTION_WINDOW:
            continue
        window = bars[index - EXECUTION_WINDOW : index + 1]
        pivots = confirmed_pivots(window, PivotConfig())
        legs_here = exhaustion_legs(window, pivots, setup.direction)
        best_legs = max(best_legs, legs_here)
        if legs_here < min_legs:
            waiting = "LEGS_SHORT"
            continue
        legs = [pivot for pivot in pivots if pivot.confirmed and pivot.kind is kind]
        if not legs:
            waiting = "NO_PIVOT"
            continue
        reference = legs[-1].price
        # The leg has to be inside the setup: a stop the wrong side of the
        # anchor is not a tighter stop, it is a different trade.
        if long and not setup.invalidation_price < reference < bar.close:
            waiting = "REFERENCE_OUTSIDE"
            continue
        if not long and not bar.close < reference < setup.invalidation_price:
            waiting = "REFERENCE_OUTSIDE"
            continue
        # A chain that was already complete when the search arrived is a
        # different thing from one that formed while we watched. The pivots
        # come from the 240 bars behind `index`, so both are possible - and
        # §33.8 says the setup only gives the search about one bar, which
        # makes the difference the whole question.
        ENTRY_CENSUS[
            "ENTERED (도착 시 이미 완성)"
            if index == start
            else "ENTERED (탐색 중 완성)"
        ] += 1
        ENTRY_BEST_LEGS[best_legs] += 1
        return index, reference
    ENTRY_CENSUS[waiting] += 1
    ENTRY_BEST_LEGS[best_legs] += 1
    return None


def _retrace_entry(
    bars: Sequence[CompletedOhlcvBar],
    confirmed: int,
    setup: HlitSetup,
    level: Decimal,
) -> int | None:
    """The bar where price returns to `level`, or None if it never does.

    Section 4.2 orders the entry: the five-minute divergence is step [1] and
    the entry is step [6], after [2] - price approaching the zone marked
    earlier. Every rung of the ladder entered at [1] instead, at the close of
    the bar that confirmed the divergence, by which time the confirmation lag
    has already spent part of the distance to the 66% target. That is what
    left three quarters of setups offering under 1R.

    Two ways to end without a trade, both structural rather than chosen:
    price breaking the anchor finishes the setup, and price reaching the
    target without us leaves nothing to enter for. `HORIZON` caps the wait at
    the same length the resolver gives a position.

    The fill is the touching bar's close, not the level. Section 4.1 is
    explicit that he does not rest a limit order at the zone, and a close is
    what every other decision here is taken on.
    """
    for index in range(confirmed + 1, min(confirmed + HORIZON, len(bars))):
        bar = bars[index]
        if setup.direction is Side.BUY:
            if bar.low <= setup.invalidation_price or bar.high >= setup.target_price:
                return None
            if bar.low <= level:
                return index
        else:
            if bar.high >= setup.invalidation_price or bar.low <= setup.target_price:
                return None
            if bar.high >= level:
                return index
    return None


def _five_minute_index(moment: datetime, five_at: Mapping[datetime, int]) -> int:
    """The five-minute bar holding `moment`, or -1 when there is none.

    A position opened and closed on the minute series still has to tell the
    five-minute loop when it is free again, and the two index spaces are not
    interchangeable.
    """
    floored = moment - timedelta(
        minutes=moment.minute % 5,
        seconds=moment.second,
        microseconds=moment.microsecond,
    )
    return five_at.get(floored, -1)


_AS_REGULAR = {
    DivergenceKind.HIDDEN_BULLISH: DivergenceKind.REGULAR_BULLISH,
    DivergenceKind.HIDDEN_BEARISH: DivergenceKind.REGULAR_BEARISH,
}


def _selected(facts: DivergenceFacts, mode: str) -> DivergenceFacts:
    """Which divergences get a retracement drawn for them.

    §13.1's H4 adds the hidden ones. The document gives no separate anchor
    construction for them - §3.1 STEP 3 wants the absolute high between two
    lows and the second low, and says nothing about which divergence
    produced the pair - so the same construction is used. `_setup` keys on
    the signal's kind, so a hidden signal has to be relabelled to reach it.

    The relabelling is mechanical and no geometry is invented here. What it
    does mean is that `hlit.build_hlit_setups` still believes it is drawing
    for a regular divergence, which is why this lives in the harness and not
    in the strategy: §22.10's runtime profile says
    `hlit_regular_divergence: true`, and H4 is a question about that, not a
    change to it.

    Regular wins a tie. §4.4 is explicit - "의심의 여지 없이 정규를
    선호합니다" - so where both exist for a direction, the regular one is
    the setup and the hidden one is not a second trade.
    """
    if mode == "regular":
        return facts
    promoted = tuple(
        replace(signal, kind=_AS_REGULAR[signal.kind])
        for signal in facts.hidden
        if signal.kind in _AS_REGULAR
    )
    regular = facts.regular if mode == "both" else ()
    taken = {signal.kind for signal in regular}
    return DivergenceFacts(
        observed_at=facts.observed_at,
        regular=regular
        + tuple(signal for signal in promoted if signal.kind not in taken),
        hidden=(),
    )


def _minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _in_session(moment: datetime, window: tuple[int, int] | None) -> bool:
    """Whether a bar falls inside the active window, in UTC.

    §7.1 puts `active_productive_work_minutes: 60` beside the US open and
    §13.2 makes the end a free parameter, `active_window_end: ["10:30",
    "11:00", "11:30"]` - one, one and a half, or two hours from 09:30 New
    York. §14.2 says the US open becomes "시장별 유동성 세션" in another
    market, and BTC's was measured at 12:00-16:00 UTC.

    A window that wraps midnight is allowed, because a liquidity session in
    another market may.
    """
    if window is None:
        return True
    start, end = window
    minute = moment.hour * 60 + moment.minute
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end


_VETO_STEPS = {"1h": 12, "1d": 288}
_SMA = (6, 70, 200)


def _higher_frame(
    bars: Sequence[CompletedOhlcvBar], step: int
) -> tuple[tuple[datetime, Decimal], ...]:
    """Close the five-minute series onto a coarser one, by whole buckets only.

    A partial bucket at the end is dropped: the regime is read from
    completed bars, the same rule the rest of this harness follows.
    """
    closes: list[tuple[datetime, Decimal]] = []
    for start in range(0, len(bars) - step + 1, step):
        closes.append((bars[start + step - 1].timestamp, bars[start + step - 1].close))
    return tuple(closes)


def _regimes(
    closes: tuple[tuple[datetime, Decimal], ...],
) -> tuple[tuple[datetime, int], ...]:
    """The §2.2 regime at each higher-frame close: +1 up, -1 down, 0 neither.

    "상승 추세 정의 (필수 전제)" is A0 and §13.2 fixes the periods at
    6/70/200 as `fixed_by_evidence`. Slope is taken against the previous
    bar, which the document does not specify - it says the 200 must rise and
    the 70 must rise, not over what distance - so the shortest honest
    reading is used and named here as a choice.

    Every value comes from bars at or before its own timestamp.
    """
    values = [price for _, price in closes]
    out: list[tuple[datetime, int]] = []
    longest = max(_SMA)
    for index in range(len(values)):
        if index < longest:
            out.append((closes[index][0], 0))
            continue

        def mean(period: int, at: int = index) -> Decimal:
            window = values[at - period + 1 : at + 1]
            return sum(window, Decimal(0)) / period

        fast_mid = mean(_SMA[1])
        slow = mean(_SMA[2])
        mid_before = mean(_SMA[1], index - 1)
        slow_before = mean(_SMA[2], index - 1)
        if slow > slow_before and fast_mid > slow and fast_mid > mid_before:
            out.append((closes[index][0], 1))
        elif slow < slow_before and fast_mid < slow and fast_mid < mid_before:
            out.append((closes[index][0], -1))
        else:
            out.append((closes[index][0], 0))
    return tuple(out)


def _regime_at(regimes: tuple[tuple[datetime, int], ...], moment: datetime) -> int:
    """The most recent higher-frame regime that had closed by `moment`."""
    low, high = 0, len(regimes)
    while low < high:
        middle = (low + high) // 2
        if regimes[middle][0] <= moment:
            low = middle + 1
        else:
            high = middle
    return regimes[low - 1][1] if low else 0


# §8: "실업/고용지표(매월 첫 금요일 14:30) → 세션 전체". Spanish 14:30 is
# 12:30 UTC in summer and 13:30 in winter; the earlier one is used so the
# window never starts after the release.
_NFP_MINUTE = 12 * 60 + 30


def _news_blocked(moment: datetime, rule: str) -> bool:
    """Whether §8's first-Friday rule blocks this bar.

    The rest of §8 needs a calendar we do not have. `block_stars: [2, 3]`
    wants every two- and three-star release for two years, and the feed the
    loop uses is `ff_calendar_thisweek.json` - this week only. What can be
    computed from the date alone is `nfp_first_friday`, which is also the
    strongest rule §8 states: block the whole session rather than minutes.

    Two readings of "세션 전체", both from the same clause and both run:
    `day` blocks the whole first Friday, `after` blocks it from the release
    onward. The document says `block_entire_session: true` and does not say
    whether the session starts before the release.
    """
    if rule == "none":
        return False
    if not (moment.weekday() == 4 and moment.day <= 7):
        return False
    if rule == "day":
        return True
    return moment.hour * 60 + moment.minute >= _NFP_MINUTE


def replay(
    bars: tuple[CompletedOhlcvBar, ...],
    *,
    gate: str,
    min_legs: int,
    entry_model: str,
    zone_model: str,
    floor_atr: Decimal = STOP_DISTANCE_MINIMUM_ATR,
    ceiling_atr: Decimal = STOP_DISTANCE_MAXIMUM_ATR,
    execution_bars: Sequence[CompletedOhlcvBar] = (),
    scale: ExecutionScale | None = None,
    pivot_left: int = PivotConfig().left,
    distances: list[dict[str, object]] | None = None,
    divergence_model: str = "regular",
    breakeven_at: Decimal | None = None,
    add_at: Decimal | None = None,
    resolve_bars: Sequence[CompletedOhlcvBar] | None = None,
    resolve_per_five: int = 1,
    session: tuple[int, int] | None = None,
    veto: str = "none",
    news: str = "none",
    invert: bool = False,
    retrace_to: str = "zone",
) -> list[dict[str, object]]:
    trades: list[dict[str, object]] = []
    refused = 0
    never_returned = 0
    no_zone = 0
    no_chain = 0
    stale = 0
    # The entry scale is a different series, so a five-minute bar has to name
    # the minute that follows its close. Its `timestamp` is the open time, so
    # the first minute we may act on opens one step later.
    execution_at = {bar.timestamp: index for index, bar in enumerate(execution_bars)}
    # `busy_until` gates the five-minute loop, so a position closed on the
    # minute series has to come back as a five-minute index. Comparing the two
    # directly is how the one-minute runs looked like they had no setups:
    # a minute index runs to a million against two hundred thousand, so the
    # first trade blocked every evaluation point after it.
    five_at = {bar.timestamp: index for index, bar in enumerate(bars)}
    executed = bool(execution_bars) and scale is not None
    horizon = HORIZON * scale.per_five_minutes if executed and scale else HORIZON
    resolve_at = (
        {bar.timestamp: index for index, bar in enumerate(resolve_bars)}
        if resolve_bars
        else {}
    )
    resolve_horizon = HORIZON * resolve_per_five
    zoned = gate == "h2" or entry_model == "retrace"
    pivots_config = PivotConfig(left=pivot_left)
    points = evaluation_points(bars, pivots_config)
    print(f"{len(bars)} bars, {len(points)} evaluation points", flush=True)
    ENTRY_CENSUS.clear()
    ENTRY_BEST_LEGS.clear()
    ENTRY_BARS.clear()
    SETUP_AT_DETECTION.clear()
    busy_until = -1
    zone_bucket = -1
    zone_facts = None
    exhaustion = None
    seen: set[tuple[str, str]] = set()
    outside_session = 0
    blocked_by_news = 0
    vetoed = 0
    regimes = (
        _regimes(_higher_frame(bars, _VETO_STEPS[veto])) if veto in _VETO_STEPS else ()
    )
    for done, cut in enumerate(points):
        if cut < WINDOW or cut <= busy_until:
            continue
        if not _in_session(bars[cut].timestamp, session):
            outside_session += 1
            continue
        if _news_blocked(bars[cut].timestamp, news):
            blocked_by_news += 1
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
        divergence = _selected(
            evaluate_divergence(aligned_bars, histogram, pivots_config),
            divergence_model,
        )
        if not divergence.regular:
            continue
        facts = build_hlit_setups(aligned_bars, divergence)
        aligned_pivots = (
            confirmed_pivots(aligned_bars, pivots_config)
            if gate in ("h1", "h2")
            else ()
        )
        if zoned:
            bucket = cut // ZONE_REFRESH
            if bucket != zone_bucket:
                zone_bucket = bucket
                zone_facts = (
                    build_hlit_zones(
                        bars[cut - ZONE_WINDOW : cut + 1],
                        ZoneConfig(source_timezone="UTC"),
                    )
                    if cut >= ZONE_WINDOW
                    else None
                )
            if zone_facts is None or not zone_facts.zones:
                continue
            if gate == "h2":
                exhaustion = evaluate_exhaustion(
                    aligned_bars, zones=zone_facts, pivots=aligned_pivots
                )
        for setup in (facts.bullish, facts.bearish):
            if setup is None:
                continue
            if gate == "h1" and (
                exhaustion_legs(aligned_bars, aligned_pivots, setup.direction)
                < min_legs
            ):
                continue
            if gate == "h2":
                sequence = (
                    exhaustion.bullish
                    if setup.direction is Side.BUY
                    else exhaustion.bearish
                )
                if sequence is None or not sequence.confirmed:
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
            if regimes:
                # §13.1's H3. A long needs the higher frame rising and a
                # short needs it falling; "neither" refuses both, because a
                # veto that abstains is not a veto.
                wanted = 1 if setup.direction is Side.BUY else -1
                if _regime_at(regimes, bars[cut].timestamp) != wanted:
                    vetoed += 1
                    continue
            seen.add(key)
            close = bars[cut].close
            if setup.direction is Side.BUY:
                past_target = close >= setup.target_price
                past_anchor = close <= setup.invalidation_price
            else:
                past_target = close <= setup.target_price
                past_anchor = close >= setup.invalidation_price
            SETUP_AT_DETECTION[
                "목표 이미 통과"
                if past_target
                else "앵커 이미 통과"
                if past_anchor
                else "정상"
            ] += 1
            # §33.9. A setup whose target is already behind the price has
            # `reward <= 0` and is discarded a few lines below, whatever
            # happens in between. Discarding it here instead changes no H0
            # number and stops the execution-scale search from spending the
            # hour looking for an entry into a trade that will not be taken -
            # and from reporting the target it already passed as the reason
            # it found nothing.
            if past_target or past_anchor:
                stale += 1
                continue
            opened = cut
            reference = setup.invalidation_price
            series: Sequence[CompletedOhlcvBar] = bars
            if entry_model == "retrace":
                assert zone_facts is not None
                if retrace_to == "zone":
                    choose = (
                        _anchor_zone_level
                        if zone_model == "anchor"
                        else _nearest_zone_level
                    )
                    level = choose(setup, zone_facts.zones, bars[cut].close)
                else:
                    level = _setup_level(setup, retrace_to)
                if level is None:
                    no_zone += 1
                    continue
                # Already at the level: the approach happened before the
                # divergence confirmed, so there is nothing to wait for.
                reached = (
                    bars[cut].low <= level
                    if setup.direction is Side.BUY
                    else bars[cut].high >= level
                )
                if not reached:
                    touched = _retrace_entry(bars, cut, setup, level)
                    if touched is None:
                        never_returned += 1
                        continue
                    opened = touched
            atr = average_true_range(window)
            if atr is None or atr <= 0:
                continue
            if executed and scale is not None:
                start = execution_at.get(bars[cut].timestamp + STEP)
                if start is None:
                    continue
                found = _execution_entry(
                    execution_bars, start, setup, min_legs, scale.entry_window
                )
                if found is None:
                    no_chain += 1
                    continue
                opened, reference = found
                series = execution_bars
            entry = series[opened].close
            if distances is not None:
                # Every setup that gets this far, whichever side of the band
                # it lands on. What the band refuses is the question, so the
                # refusals have to be in the record too.
                raw = (
                    entry - reference
                    if setup.direction is Side.BUY
                    else reference - entry
                )
                room = (
                    setup.target_price - entry
                    if setup.direction is Side.BUY
                    else entry - setup.target_price
                )
                distances.append(
                    {
                        "stop_atr5m": float(raw / atr),
                        "reward_atr5m": float(room / atr),
                        "r_at_this_stop": float(room / raw) if raw > 0 else None,
                        "side": setup.direction.value,
                        "at": series[opened].timestamp.isoformat(),
                    }
                )
            stop = _stop_price(
                entry, reference, setup.direction, atr, floor_atr, ceiling_atr
            )
            if stop is None:
                refused += 1
                continue
            target = setup.target_price
            risk = entry - stop if setup.direction is Side.BUY else stop - entry
            reward = target - entry if setup.direction is Side.BUY else entry - target
            if risk <= 0 or reward <= 0:
                continue
            # §34.4. Take the other side of the same signal.
            #
            # Reflected about the entry rather than swapped: the stop keeps
            # its distance and the target keeps its distance, so `risk` and
            # `reward` are unchanged and an R here means what an R means in
            # the baseline. Swapping the two levels instead would trade a
            # different size on a different ratio and answer a second
            # question at the same time.
            #
            # Negating the recorded R would not have done this at all. A win
            # averages +0.53R against a 0.73R target and a loss is the full
            # -1R, so the outcomes are not each other's mirror; only walking
            # the reflected levels bar by bar says what the other side did.
            direction = setup.direction
            if invert:
                if direction is Side.BUY:
                    direction = Side.SELL
                    stop, target = entry + risk, entry - reward
                else:
                    direction = Side.BUY
                    stop, target = entry - risk, entry + reward
            # §30.9's X1. `--execution-scale` moves the whole entry search
            # to the fine series, and its exhaustion chain almost never forms
            # there, so it answers a different question than the one asked.
            # This keeps detection and entry on five minutes and walks the
            # stop and the target on the fine series, which is the only part
            # the intrabar ambiguity of §30.6 lives in.
            resolution, at = series, opened
            if resolve_bars and not executed:
                after = series[opened].timestamp + STEP
                fine = resolve_at.get(after)
                if fine is None:
                    continue
                # `resolve` starts at `at + 1`, and the first bar we may act
                # on is the one that opens as the signal bar closes.
                resolution, at = resolve_bars, fine - 1
            outcome, closed, best, worst, units, gained = resolve(
                resolution,
                at,
                direction,
                entry,
                stop,
                target,
                resolve_horizon if resolution is resolve_bars else horizon,
                breakeven_at,
                add_at,
                atr,
            )
            trades.append(
                {
                    "opened_at": series[opened].timestamp.isoformat(),
                    "side": direction.value,
                    "outcome": outcome,
                    "r_target": float(reward / risk),
                    # Whatever the position actually made, over the risk
                    # the first unit started with. A stop-out is -1R only
                    # while the stop has not moved, which is the whole
                    # question S1 and P1 ask.
                    "r_result": float(gained / risk),
                    "units": int(units),
                    "bars_held": closed - at,
                    "waited": opened - cut if not executed else opened - start,
                    "mfe_r": float(best / risk),
                    "mae_r": float(worst / risk),
                }
            )
            busy_until = _five_minute_index(resolution[closed].timestamp, five_at)
            break
    if SETUP_AT_DETECTION:
        total = sum(SETUP_AT_DETECTION.values())
        print("", flush=True)
        print(f"탐지 봉에서의 셋업 상태 {total:,}건", flush=True)
        for state, count in sorted(
            SETUP_AT_DETECTION.items(), key=lambda item: -item[1]
        ):
            print(f"  {state:<16}{count:>8,}{count / total * 100:>7.1f}%", flush=True)
    if ENTRY_CENSUS:
        total = sum(ENTRY_CENSUS.values())
        print(f"\n실행 스케일 진입 탐색 {total:,}건", flush=True)
        for reason, count in sorted(ENTRY_CENSUS.items(), key=lambda item: -item[1]):
            print(
                f"  {reason:<20}{count:>8,}{count / total * 100:>7.1f}%",
                flush=True,
            )
        if ENTRY_BARS:
            ordered = sorted(ENTRY_BARS)
            middle = ordered[len(ordered) // 2]
            print(
                f"탐색이 끝나기까지 지난 봉: 중앙값 {middle}, "
                f"평균 {sum(ordered) / len(ordered):.1f}",
                flush=True,
            )
        reached = sum(ENTRY_BEST_LEGS.values())
        print(f"도달한 최대 다리 수 ({reached:,}건, 조기 종료 제외)", flush=True)
        for legs in sorted(ENTRY_BEST_LEGS):
            count = ENTRY_BEST_LEGS[legs]
            print(
                f"  {legs:>2}개{count:>12,}{count / reached * 100:>7.1f}%",
                flush=True,
            )
    if session is not None:
        print(f"세션 밖이라 건너뛴 평가 지점 {outside_session}", flush=True)
    if news != "none":
        print(f"뉴스로 막힌 평가 지점 {blocked_by_news}", flush=True)
    if regimes:
        print(f"상위 프레임이 거부한 셋업 {vetoed}", flush=True)
    print(f"손절 거리로 거부된 셋업 {refused}", flush=True)
    if executed:
        print(f"탐지 시점에 이미 끝난 셋업 {stale}", flush=True)
    print(f"실행 스케일에서 소진이 안 나온 셋업 {no_chain}", flush=True)
    if entry_model == "retrace":
        print(f"존으로 돌아오지 않은 셋업 {never_returned}", flush=True)
        print(f"해당 존이 없는 셋업 {no_zone}", flush=True)
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
    parser.add_argument("--gate", choices=("h0", "h1", "h2"), default="h0")
    parser.add_argument("--min-legs", type=int, default=3)
    parser.add_argument("--entry", choices=("close", "retrace"), default="close")
    parser.add_argument("--zone", choices=("nearest", "anchor"), default="anchor")
    parser.add_argument(
        "--stop-min-atr", type=Decimal, default=STOP_DISTANCE_MINIMUM_ATR
    )
    parser.add_argument(
        "--stop-max-atr", type=Decimal, default=STOP_DISTANCE_MAXIMUM_ATR
    )
    parser.add_argument(
        "--execution-scale", choices=("5m", *EXECUTION_SCALES), default="5m"
    )
    # Unlike `--execution-scale`, this leaves detection and entry on five
    # minutes and only walks the stop and the target finer. See §30.9.
    parser.add_argument(
        "--resolve-scale", choices=("5m", *EXECUTION_SCALES), default="5m"
    )
    # §33.21. The thirty-second series only reaches back as far as the tape,
    # so comparing it against a two-year 1m run would be comparing the scales
    # and the sample at once. These clip the execution series to one span, so
    # every scale is asked about the same setups.
    # §34.4. Trade the other side of every setup, levels reflected about the
    # entry so the risk and the reward keep their distances.
    parser.add_argument("--invert", action="store_true")
    # §12.5's second axis, which §12.4 marked "재구현 필요". Only read when
    # `--entry retrace`; "zone" keeps the two readings section 11.4 ran.
    parser.add_argument(
        "--retrace-to",
        choices=("zone", "fib50", "fib25", "anchor_b"),
        default="zone",
    )
    parser.add_argument("--execution-from", default=None)
    parser.add_argument("--execution-to", default=None)
    parser.add_argument("--pivot-left", type=int, default=PivotConfig().left)
    parser.add_argument("--distances", default=None)
    parser.add_argument(
        "--divergence", choices=("regular", "hidden", "both"), default="regular"
    )
    # "HH:MM-HH:MM" in UTC, or absent for no window.
    parser.add_argument("--session", default=None)
    # §13.1's S1 and P1. Absent means neither rule is applied, which is what
    # every run before these existed did.
    parser.add_argument("--breakeven-at", type=Decimal, default=None)
    parser.add_argument("--add-at", type=Decimal, default=None)
    parser.add_argument("--veto", choices=("none", "1h", "1d"), default="none")
    parser.add_argument("--news", choices=("none", "day", "after"), default="none")
    arguments = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    cache = root / "build" / f"klines-{arguments.symbol}-{arguments.days}d.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    bars = await fetch(arguments.symbol, arguments.days, cache)
    execution_bars: tuple[CompletedOhlcvBar, ...] = ()
    scale = EXECUTION_SCALES.get(arguments.execution_scale)
    if scale is not None:
        name = scale.cache.format(symbol=arguments.symbol, days=arguments.days)
        execution_cache = root / "build" / name
        if not execution_cache.exists():
            raise SystemExit(f"no execution cache at {execution_cache}")
        execution_bars = await fetch(
            arguments.symbol,
            arguments.days,
            execution_cache,
            scale.interval or arguments.execution_scale,
        )
    if execution_bars and (arguments.execution_from or arguments.execution_to):
        start_at = (
            datetime.fromisoformat(arguments.execution_from)
            if arguments.execution_from
            else execution_bars[0].timestamp
        )
        end_at = (
            datetime.fromisoformat(arguments.execution_to)
            if arguments.execution_to
            else execution_bars[-1].timestamp
        )
        kept = tuple(
            bar for bar in execution_bars if start_at <= bar.timestamp <= end_at
        )
        print(
            f"실행 계열을 {start_at} → {end_at} 로 자름: "
            f"{len(execution_bars):,} → {len(kept):,}봉",
            flush=True,
        )
        execution_bars = kept
    resolve_bars: tuple[CompletedOhlcvBar, ...] = ()
    resolve_scale = EXECUTION_SCALES.get(arguments.resolve_scale)
    if resolve_scale is not None:
        if scale is not None:
            # `--execution-scale` already resolves on its own series, and two
            # answers to "which series is this trade walked on" is one too
            # many.
            raise SystemExit("--resolve-scale and --execution-scale are exclusive")
        name = resolve_scale.cache.format(symbol=arguments.symbol, days=arguments.days)
        resolve_cache = root / "build" / name
        if not resolve_cache.exists() and arguments.resolve_scale == "30s":
            raise SystemExit(f"no resolve cache at {resolve_cache}")
        resolve_bars = await fetch(
            arguments.symbol, arguments.days, resolve_cache, arguments.resolve_scale
        )
    print(f"{bars[0].timestamp} → {bars[-1].timestamp}", flush=True)
    print(
        f"gate {arguments.gate}, entry {arguments.entry}"
        + (f", zone {arguments.zone}" if arguments.entry == "retrace" else "")
        + f", stop {arguments.stop_min_atr}-{arguments.stop_max_atr} ATR"
        + f", execution {arguments.execution_scale}"
        + (
            f", resolve {arguments.resolve_scale}"
            if arguments.resolve_scale != "5m"
            else ""
        )
        + f", pivot left {arguments.pivot_left}"
        + f", divergence {arguments.divergence}"
        + (f", session {arguments.session} UTC" if arguments.session else "")
        + (f", veto {arguments.veto}" if arguments.veto != "none" else "")
        + (f", news {arguments.news}" if arguments.news != "none" else "")
        + (f", BE {arguments.breakeven_at}R" if arguments.breakeven_at else "")
        + (f", add {arguments.add_at} ATR" if arguments.add_at else "")
        + (f", min_legs {arguments.min_legs}" if arguments.gate == "h1" else ""),
        flush=True,
    )
    distances: list[dict[str, object]] | None = [] if arguments.distances else None
    trades = replay(
        bars,
        gate=arguments.gate,
        min_legs=arguments.min_legs,
        entry_model=arguments.entry,
        zone_model=arguments.zone,
        floor_atr=arguments.stop_min_atr,
        ceiling_atr=arguments.stop_max_atr,
        execution_bars=execution_bars,
        scale=scale,
        pivot_left=arguments.pivot_left,
        distances=distances,
        divergence_model=arguments.divergence,
        veto=arguments.veto,
        news=arguments.news,
        invert=arguments.invert,
        retrace_to=arguments.retrace_to,
        breakeven_at=arguments.breakeven_at,
        add_at=arguments.add_at,
        resolve_bars=resolve_bars,
        resolve_per_five=resolve_scale.per_five_minutes if resolve_scale else 1,
        session=(
            tuple(_minutes(part) for part in arguments.session.split("-"))
            if arguments.session
            else None
        ),
    )
    if arguments.distances is not None and distances is not None:
        (root / arguments.distances).write_text(
            json.dumps(distances, indent=1), encoding="utf-8"
        )
        print(f"손절 거리 {len(distances)}건 기록", flush=True)
    (root / arguments.out).write_text(json.dumps(trades, indent=1), encoding="utf-8")
    report(trades)


if __name__ == "__main__":
    asyncio.run(main())
