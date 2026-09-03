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
from collections.abc import Mapping, Sequence
from dataclasses import replace
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
# One hour of one-minute bars. §4.2 drops to the execution scale straight
# after the five-minute signal and enters there; an entry an hour later is a
# different trade, so the search gives up rather than taking it.
ENTRY_WINDOW = 60
# Enough one-minute bars for the pivots the chain is built from.
EXECUTION_WINDOW = 240


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
) -> tuple[str, int, Decimal, Decimal]:
    """Walk forward until the stop or the target is touched.

    The horizon is in bars of whatever series is passed, so a minute series
    gets five times as many for the same day.
    """
    best = worst = Decimal(0)
    for index in range(opened + 1, min(opened + 1 + horizon, len(bars))):
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
    return "scratch", min(opened + horizon, len(bars) - 1), best, worst


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
    minutes: Sequence[CompletedOhlcvBar],
    start: int,
    setup: HlitSetup,
    min_legs: int,
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
    for index in range(start, min(start + ENTRY_WINDOW, len(minutes))):
        bar = minutes[index]
        if long:
            if bar.low <= setup.invalidation_price:
                return None
            if bar.high >= setup.target_price:
                return None
        else:
            if bar.high >= setup.invalidation_price:
                return None
            if bar.low <= setup.target_price:
                return None
        if index < EXECUTION_WINDOW:
            continue
        window = minutes[index - EXECUTION_WINDOW : index + 1]
        pivots = confirmed_pivots(window, PivotConfig())
        if exhaustion_legs(window, pivots, setup.direction) < min_legs:
            continue
        legs = [pivot for pivot in pivots if pivot.confirmed and pivot.kind is kind]
        if not legs:
            continue
        reference = legs[-1].price
        # The leg has to be inside the setup: a stop the wrong side of the
        # anchor is not a tighter stop, it is a different trade.
        if long and not setup.invalidation_price < reference < bar.close:
            continue
        if not long and not bar.close < reference < setup.invalidation_price:
            continue
        return index, reference
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


def replay(
    bars: tuple[CompletedOhlcvBar, ...],
    *,
    gate: str,
    min_legs: int,
    entry_model: str,
    zone_model: str,
    floor_atr: Decimal = STOP_DISTANCE_MINIMUM_ATR,
    ceiling_atr: Decimal = STOP_DISTANCE_MAXIMUM_ATR,
    minutes: Sequence[CompletedOhlcvBar] = (),
    pivot_left: int = PivotConfig().left,
    distances: list[dict[str, object]] | None = None,
    divergence_model: str = "regular",
) -> list[dict[str, object]]:
    trades: list[dict[str, object]] = []
    refused = 0
    never_returned = 0
    no_zone = 0
    no_chain = 0
    # The entry scale is a different series, so a five-minute bar has to name
    # the minute that follows its close. Its `timestamp` is the open time, so
    # the first minute we may act on opens one step later.
    minute_at = {bar.timestamp: index for index, bar in enumerate(minutes)}
    # `busy_until` gates the five-minute loop, so a position closed on the
    # minute series has to come back as a five-minute index. Comparing the two
    # directly is how the one-minute runs looked like they had no setups:
    # a minute index runs to a million against two hundred thousand, so the
    # first trade blocked every evaluation point after it.
    five_at = {bar.timestamp: index for index, bar in enumerate(bars)}
    executed = bool(minutes)
    horizon = HORIZON * 5 if executed else HORIZON
    zoned = gate == "h2" or entry_model == "retrace"
    pivots_config = PivotConfig(left=pivot_left)
    points = evaluation_points(bars, pivots_config)
    print(f"{len(bars)} bars, {len(points)} evaluation points", flush=True)
    busy_until = -1
    zone_bucket = -1
    zone_facts = None
    exhaustion = None
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
            seen.add(key)
            opened = cut
            reference = setup.invalidation_price
            series: Sequence[CompletedOhlcvBar] = bars
            if entry_model == "retrace":
                assert zone_facts is not None
                choose = (
                    _anchor_zone_level
                    if zone_model == "anchor"
                    else _nearest_zone_level
                )
                level = choose(setup, zone_facts.zones, bars[cut].close)
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
            if executed:
                start = minute_at.get(bars[cut].timestamp + STEP)
                if start is None:
                    continue
                found = _execution_entry(minutes, start, setup, min_legs)
                if found is None:
                    no_chain += 1
                    continue
                opened, reference = found
                series = minutes
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
            outcome, closed, best, worst = resolve(
                series, opened, setup.direction, entry, stop, target, horizon
            )
            trades.append(
                {
                    "opened_at": series[opened].timestamp.isoformat(),
                    "side": setup.direction.value,
                    "outcome": outcome,
                    "r_target": float(reward / risk),
                    "r_result": {"win": float(reward / risk), "loss": -1.0}.get(
                        outcome,
                        float((series[closed].close - entry) / risk)
                        if setup.direction is Side.BUY
                        else float((entry - series[closed].close) / risk),
                    ),
                    "bars_held": closed - opened,
                    "waited": opened - cut if not executed else opened - start,
                    "mfe_r": float(best / risk),
                    "mae_r": float(worst / risk),
                }
            )
            busy_until = _five_minute_index(series[closed].timestamp, five_at)
            break
    print(f"손절 거리로 거부된 셋업 {refused}", flush=True)
    if executed:
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
    parser.add_argument("--execution-scale", choices=("5m", "1m"), default="5m")
    parser.add_argument("--pivot-left", type=int, default=PivotConfig().left)
    parser.add_argument("--distances", default=None)
    parser.add_argument(
        "--divergence", choices=("regular", "hidden", "both"), default="regular"
    )
    arguments = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    cache = root / "build" / f"klines-{arguments.symbol}-{arguments.days}d.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    bars = await fetch(arguments.symbol, arguments.days, cache)
    minutes: tuple[CompletedOhlcvBar, ...] = ()
    if arguments.execution_scale == "1m":
        minute_cache = (
            root / "build" / f"klines-{arguments.symbol}-1m-{arguments.days}d.json"
        )
        if not minute_cache.exists():
            raise SystemExit(f"no minute cache at {minute_cache}")
        minutes = await fetch(arguments.symbol, arguments.days, minute_cache)
    print(f"{bars[0].timestamp} → {bars[-1].timestamp}", flush=True)
    print(
        f"gate {arguments.gate}, entry {arguments.entry}"
        + (f", zone {arguments.zone}" if arguments.entry == "retrace" else "")
        + f", stop {arguments.stop_min_atr}-{arguments.stop_max_atr} ATR"
        + f", execution {arguments.execution_scale}"
        + f", pivot left {arguments.pivot_left}"
        + f", divergence {arguments.divergence}"
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
        minutes=minutes,
        pivot_left=arguments.pivot_left,
        distances=distances,
        divergence_model=arguments.divergence,
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
