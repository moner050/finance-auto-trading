from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from itertools import pairwise
from typing import cast

from autotrader.domain.enums import Side
from autotrader.shared.decimal import require_decimal
from autotrader.strategies.david_v6.models import EvidenceState

_CLUSTER_GAP = timedelta(milliseconds=150)
_THIRTY_SECONDS = timedelta(seconds=30)


class AggressorSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class BigTradeClass(StrEnum):
    NORMAL = "NORMAL"
    EXTREME = "EXTREME"


@dataclass(frozen=True, slots=True)
class TradePrint:
    provider_trade_id: str
    occurred_at: datetime
    price: Decimal | None
    quantity: Decimal | None
    buyer_maker: bool | None

    def __post_init__(self) -> None:
        if (
            type(self.provider_trade_id) is not str
            or not self.provider_trade_id
            or self.provider_trade_id.strip() != self.provider_trade_id
        ):
            raise ValueError("provider_trade_id must be non-empty trimmed text")
        _require_utc(self.occurred_at, "occurred_at")
        for name in ("price", "quantity"):
            value = getattr(self, name)
            if value is None:
                continue
            normalized = require_decimal(value)
            if normalized <= 0:
                raise ValueError(f"trade {name} must be positive")
            object.__setattr__(self, name, normalized)
        if self.buyer_maker is not None and type(self.buyer_maker) is not bool:
            raise TypeError("buyer_maker must be bool or None")

    @property
    def side(self) -> AggressorSide | None:
        if self.buyer_maker is None:
            return None
        return AggressorSide.SELL if self.buyer_maker else AggressorSide.BUY

    @property
    def notional(self) -> Decimal | None:
        if self.price is None or self.quantity is None:
            return None
        return self.price * self.quantity


# What makes a Big Trade big, per section 22.5.
#
# ATAS marks these with an Auto Filter rather than a contract count, and
# section 19.1 says so about picking one: "고정값 하나를 선택하는 대신 'RTH 한
# 세션당 의미 있는 마커가 몇 개 나오는가'를 기준으로 조정하는 것이 낫다",
# with the goal being "실제로 경로를 막는 소수의 이벤트만". A filter that keeps
# the top of the distribution does that by construction; a typed notional stops
# doing it the moment the market changes character.
#
# Section 22.5 gives the crypto normalization exactly:
#
#     normal_big_trade  = event_notional >= rolling_quantile(0.995)
#     extreme_big_trade = event_notional >= rolling_quantile(0.999)
#
# taken over aggregated events, not over single prints.
BIG_TRADE_NORMAL_QUANTILE = Decimal("0.995")
BIG_TRADE_EXTREME_QUANTILE = Decimal("0.999")

# Below this the quantile describes the sample rather than the market. At two
# hundred events the top half-percent is one event, which is the fewest that
# can still be called a selection; under it the "biggest of six" would be
# marked an institutional obstacle.
MINIMUM_BIG_TRADE_EVENTS = 200

# Section 22.5's second control, in its own words: "고정 백분위도 시장 상태에
# 따라 과다·과소 검출될 수 있으므로 세션당 이벤트 수를 함께 통제한다", with
# `target_events_per_liquidity_session: [5, 10, 20]`.
#
# It matters where the distribution is flat. A quantile compares with `>=`, so
# a window whose events are all the same size marks every one of them as an
# institutional obstacle - the opposite of "실제로 경로를 막는 소수의 이벤트만".
#
# Twenty is the top of the documented grid, and the top is the right end for
# this rule: these markers only ever refuse an entry, so a larger cap refuses
# more, and a smaller one would trade the safety away for tidiness.
#
# The unit was wrong, though, and F12 found it: the grid counts events per
# **liquidity session** and this counted them per evaluation window. The
# session was measured at four hours - 12:00-16:00 UTC, where BTCUSDT's
# hourly notional sits half again above its median - and the window is
# thirty minutes, so twenty a session is two and a half a window, not twenty.
# Applied per window it was eight times the top of a grid that is already
# the top.
#
# So the constant is the document's number, in the document's unit, and the
# cap is derived. Rounded up, on the same reasoning the paragraph above
# gives: a larger cap refuses more, and refusing is what these markers do.
BIG_TRADE_EVENTS_PER_LIQUIDITY_SESSION = 20
LIQUIDITY_SESSION = timedelta(hours=4)
BIG_TRADE_WINDOW = timedelta(minutes=30)
MAXIMUM_BIG_TRADE_MARKERS = math.ceil(
    BIG_TRADE_EVENTS_PER_LIQUIDITY_SESSION * (BIG_TRADE_WINDOW / LIQUIDITY_SESSION)
)

# What counts as an extreme delta, and why it is not a typed notional either.
#
# The name says it: `delta_p90` is the ninetieth percentile of delta. It gates
# the MIG and secado observations, both of which section 15.2 holds at
# `score_only`, and a fixed notional stops describing "extreme for this tape"
# the moment volume changes character - the same objection section 19.1 raises
# to picking a contract count for Big Trades.
#
# Ranked over the window's own thirty-second bars, so it moves with the tape.
DELTA_QUANTILE = Decimal("0.90")

# Under twenty bars a ninetieth percentile is the second largest of a handful.
# Below it the MIG and secado observations are absent rather than computed
# against a threshold that describes the sample.
MINIMUM_DELTA_BARS = 20


class BigTradesUnmeasured(RuntimeError):
    """Raised when a window held too few events to rank one against them."""


@dataclass(frozen=True, slots=True)
class OrderFlowThresholds:
    tick_size: Decimal
    atr_30s: Decimal
    # Section 15.2 records Ceros osmóticos as undisclosed, confidence LOW, and
    # `telemetry_only`. Nothing here reads the result, so requiring these
    # would make an operator invent two numbers the author never published in
    # order to compute a field no decision consults. Absent means the
    # telemetry is absent, which is the truth about it.
    ceros_near_zero_notional: Decimal | None = None
    ceros_large_notional: Decimal | None = None

    def __post_init__(self) -> None:
        for name in (
            "tick_size",
            "atr_30s",
        ):
            value = require_decimal(getattr(self, name))
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        for name in ("ceros_near_zero_notional", "ceros_large_notional"):
            found = getattr(self, name)
            if found is None:
                continue
            value = require_decimal(found)
            if value <= 0:
                raise ValueError(f"{name} must be positive when present")
            object.__setattr__(self, name, value)
        if (self.ceros_near_zero_notional is None) != (
            self.ceros_large_notional is None
        ):
            raise ValueError("both Ceros thresholds are present or neither is")
        if (
            self.ceros_large_notional is not None
            and self.ceros_near_zero_notional is not None
            and self.ceros_large_notional < self.ceros_near_zero_notional
        ):
            raise ValueError("Ceros large threshold cannot be below near-zero")


@dataclass(frozen=True, slots=True)
class BigTradeCluster:
    side: AggressorSide
    started_at: datetime
    ended_at: datetime
    low_price: Decimal
    high_price: Decimal
    trade_count: int
    summed_notional: Decimal
    classification: BigTradeClass


@dataclass(frozen=True, slots=True)
class OrderFlowFacts:
    state: EvidenceState
    trade_count: int
    unknown_aggressor_count: int
    buy_notional: Decimal
    sell_notional: Decimal
    delta_notional: Decimal | None
    # None means the window held too few events for section 22.5's quantile
    # to mean anything - not that there were no obstacles in it.
    big_trades: tuple[BigTradeCluster, ...] | None
    reversal_mig: bool | None
    continuation_mig: bool | None
    secado: bool | None
    ceros: bool | None
    telemetry_only: bool


@dataclass(frozen=True, slots=True)
class _FlowBar:
    index: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    delta: Decimal | None
    point_of_control: Decimal


def aggregate_order_flow(
    trades: Sequence[TradePrint],
    *,
    window_start: datetime,
    window_end: datetime,
    thresholds: OrderFlowThresholds,
) -> OrderFlowFacts:
    start = _require_utc(window_start, "window_start")
    end = _require_utc(window_end, "window_end")
    if end <= start:
        raise ValueError("order-flow window must be positive")
    if type(cast(object, thresholds)) is not OrderFlowThresholds:
        raise TypeError("thresholds must be exact OrderFlowThresholds")
    selected = tuple(
        trade
        for trade in trades
        if type(trade) is TradePrint and start <= trade.occurred_at < end
    )
    if any(type(trade) is not TradePrint for trade in trades):
        raise TypeError("trades must contain exact TradePrint values")
    deduplicated = _deduplicate(selected)
    if any(trade.notional is None for trade in deduplicated):
        return _unavailable()
    known = tuple(trade for trade in deduplicated if trade.side is not None)
    buy_notional = sum(
        (
            cast(Decimal, trade.notional)
            for trade in known
            if trade.side is AggressorSide.BUY
        ),
        start=Decimal(0),
    )
    sell_notional = sum(
        (
            cast(Decimal, trade.notional)
            for trade in known
            if trade.side is AggressorSide.SELL
        ),
        start=Decimal(0),
    )
    unknown_count = len(deduplicated) - len(known)
    bars = _flow_bars(deduplicated, start, end)
    delta_threshold = _delta_threshold(bars)
    # Only the Big Trades go missing when the sample is short. MIG, secado and
    # the ceros are read off the bars and the prints, and none of them is
    # ranked against anything, so taking the whole record down with the
    # quantile would hide facts that were measured.
    return OrderFlowFacts(
        state=(EvidenceState.UNKNOWN if unknown_count else EvidenceState.AVAILABLE),
        trade_count=len(deduplicated),
        unknown_aggressor_count=unknown_count,
        buy_notional=buy_notional,
        sell_notional=sell_notional,
        delta_notional=(None if unknown_count else buy_notional - sell_notional),
        big_trades=_big_trades(known, thresholds),
        reversal_mig=_reversal_mig(bars, thresholds, delta_threshold),
        continuation_mig=_continuation_mig(bars, thresholds, delta_threshold),
        secado=_secado(bars, thresholds, delta_threshold),
        ceros=_ceros(deduplicated, thresholds),
        telemetry_only=True,
    )


def _deduplicate(trades: tuple[TradePrint, ...]) -> tuple[TradePrint, ...]:
    by_id: dict[str, TradePrint] = {}
    for trade in trades:
        existing = by_id.get(trade.provider_trade_id)
        if existing is not None and existing != trade:
            raise ValueError("provider trade identity payload collision")
        by_id[trade.provider_trade_id] = trade
    return tuple(
        sorted(
            by_id.values(),
            key=lambda trade: (trade.occurred_at, trade.provider_trade_id),
        )
    )


def _events(
    trades: tuple[TradePrint, ...], thresholds: OrderFlowThresholds
) -> tuple[tuple[TradePrint, ...], ...]:
    """Section 22.5's aggregation: same aggressor, 150ms apart, within 2 ticks.

    ATAS calls this Cumulative Trades, and section 19.1 marks it the most
    likely of the two calculation modes. It is what makes an event rather than
    a print the thing being sized: an institution working an order leaves many
    prints, and measuring them separately hides exactly the participant the
    marker exists to find.
    """
    groups: list[list[TradePrint]] = []
    for trade in trades:
        if not groups:
            groups.append([trade])
            continue
        group = groups[-1]
        prices = tuple(cast(Decimal, item.price) for item in (*group, trade))
        if (
            trade.side is group[-1].side
            and trade.occurred_at - group[-1].occurred_at <= _CLUSTER_GAP
            and max(prices) - min(prices) <= Decimal(2) * thresholds.tick_size
        ):
            group.append(trade)
        else:
            groups.append([trade])
    return tuple(tuple(group) for group in groups)


def _event_notional(group: tuple[TradePrint, ...]) -> Decimal:
    return sum((cast(Decimal, trade.notional) for trade in group), start=Decimal(0))


def big_trade_quantile(notionals: Sequence[Decimal], quantile: Decimal) -> Decimal:
    """The nearest-rank value at `quantile` over the window's own events.

    Nearest rank rather than an interpolated one: an interpolated quantile
    invents a notional that no event had, and this number is compared against
    event notionals to decide which of them is an obstacle.
    """
    ordered = sorted(notionals)
    if not ordered:
        raise ValueError("a quantile needs at least one value")
    rank = (quantile * Decimal(len(ordered))).to_integral_value(rounding=ROUND_CEILING)
    index = max(1, min(len(ordered), int(rank)))
    return ordered[index - 1]


def _big_trades(
    trades: tuple[TradePrint, ...], thresholds: OrderFlowThresholds
) -> tuple[BigTradeCluster, ...] | None:
    """The window's obstacles, or None when the window cannot say.

    None is not "no big trades". Reporting an empty tuple from too small a
    sample would clear the path for an entry that nothing had actually looked
    for an obstacle in, which is the one direction this must never fail.
    """
    groups = _events(trades, thresholds)
    if len(groups) < MINIMUM_BIG_TRADE_EVENTS:
        return None
    notionals = tuple(_event_notional(group) for group in groups)
    normal = big_trade_quantile(notionals, BIG_TRADE_NORMAL_QUANTILE)
    extreme = big_trade_quantile(notionals, BIG_TRADE_EXTREME_QUANTILE)
    selected = sorted(
        ((summed, index) for index, summed in enumerate(notionals) if summed >= normal),
        reverse=True,
    )
    # Largest first, then back into time order, so the cap keeps the biggest
    # events rather than the earliest ones.
    keep = {index for _, index in selected[:MAXIMUM_BIG_TRADE_MARKERS]}
    clusters: list[BigTradeCluster] = []
    for index, (group, summed) in enumerate(zip(groups, notionals, strict=True)):
        if index not in keep:
            continue
        prices = tuple(cast(Decimal, trade.price) for trade in group)
        side = group[0].side
        assert side is not None
        clusters.append(
            BigTradeCluster(
                side=side,
                started_at=group[0].occurred_at,
                ended_at=group[-1].occurred_at,
                low_price=min(prices),
                high_price=max(prices),
                trade_count=len(group),
                summed_notional=summed,
                classification=(
                    BigTradeClass.EXTREME if summed >= extreme else BigTradeClass.NORMAL
                ),
            )
        )
    return tuple(clusters)


def _flow_bars(
    trades: tuple[TradePrint, ...],
    start: datetime,
    end: datetime,
) -> tuple[_FlowBar, ...]:
    groups: dict[int, list[TradePrint]] = {}
    for trade in trades:
        index = int((trade.occurred_at - start) // _THIRTY_SECONDS)
        groups.setdefault(index, []).append(trade)
    bars: list[_FlowBar] = []
    for index, group in sorted(groups.items()):
        if start + (index + 1) * _THIRTY_SECONDS > end:
            continue
        prices = tuple(cast(Decimal, trade.price) for trade in group)
        quantities = tuple(cast(Decimal, trade.quantity) for trade in group)
        by_price: dict[Decimal, Decimal] = {}
        for trade in group:
            price = cast(Decimal, trade.price)
            by_price[price] = by_price.get(price, Decimal(0)) + cast(
                Decimal, trade.notional
            )
        complete_delta = all(trade.side is not None for trade in group)
        delta = None
        if complete_delta:
            delta = sum(
                (
                    cast(Decimal, trade.notional)
                    * (Decimal(1) if trade.side is AggressorSide.BUY else Decimal(-1))
                    for trade in group
                ),
                start=Decimal(0),
            )
        bars.append(
            _FlowBar(
                index=index,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=sum(quantities, start=Decimal(0)),
                delta=delta,
                point_of_control=min(
                    (
                        price
                        for price, value in by_price.items()
                        if value == max(by_price.values())
                    )
                ),
            )
        )
    return tuple(bars)


THIRTY_SECOND_ATR_WINDOW = 14


def thirty_second_atr(
    trades: Sequence[TradePrint],
    *,
    window_start: datetime,
    window_end: datetime,
    window: int = THIRTY_SECOND_ATR_WINDOW,
) -> Decimal | None:
    """The average true range of the thirty-second bars, or None.

    The order-flow rules measure progress against this, and the thirty-second
    bar is defined here rather than anywhere else - built from the trade tape,
    because the venue publishes no kline shorter than a minute. Exposed so a
    caller does not have to rebuild the same aggregation and get a slightly
    different one.
    """
    start = _require_utc(window_start, "window_start")
    end = _require_utc(window_end, "window_end")
    if end <= start:
        raise ValueError("the thirty-second window must be positive")
    selected = tuple(
        trade
        for trade in trades
        if type(trade) is TradePrint and start <= trade.occurred_at < end
    )
    bars = _flow_bars(_deduplicate(selected), start, end)
    if len(bars) <= window:
        return None
    ranges = [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in pairwise(bars[-window - 1 :])
    ]
    average = sum(ranges, start=Decimal(0)) / Decimal(len(ranges))
    return average if average > 0 else None


def _delta_threshold(bars: tuple[_FlowBar, ...]) -> Decimal | None:
    """What an extreme delta is on this tape, or None when it cannot say."""
    magnitudes = tuple(abs(bar.delta) for bar in bars if bar.delta is not None)
    if len(magnitudes) < MINIMUM_DELTA_BARS:
        return None
    return big_trade_quantile(magnitudes, DELTA_QUANTILE)


def _reversal_mig(
    bars: tuple[_FlowBar, ...],
    thresholds: OrderFlowThresholds,
    delta_threshold: Decimal | None,
) -> bool | None:
    if delta_threshold is None:
        # Too few bars to say what an extreme delta is here, so the
        # observation is absent rather than measured against a guess.
        return None
    if len(bars) < 2 or any(bar.delta is None for bar in bars):
        return None
    for event, confirmation in pairwise(bars):
        if confirmation.index != event.index + 1 or event.delta is None:
            continue
        span = event.high - event.low
        if span <= 0 or abs(event.delta) < delta_threshold:
            continue
        progress = abs(event.close - event.open)
        if progress > Decimal("0.15") * thresholds.atr_30s:
            continue
        if event.delta < 0:
            wick = (min(event.open, event.close) - event.low) / span
            close_location = (event.close - event.low) / span
            if (
                wick >= Decimal("0.35")
                and close_location >= Decimal("0.65")
                and confirmation.high > event.high
            ):
                return True
        else:
            wick = (event.high - max(event.open, event.close)) / span
            close_location = (event.close - event.low) / span
            if (
                wick >= Decimal("0.35")
                and close_location <= Decimal("0.35")
                and confirmation.low < event.low
            ):
                return True
    return False


def _continuation_mig(
    bars: tuple[_FlowBar, ...],
    thresholds: OrderFlowThresholds,
    delta_threshold: Decimal | None,
) -> bool | None:
    del thresholds, delta_threshold
    if len(bars) < 2 or any(bar.delta is None for bar in bars):
        return None
    for event, confirmation in pairwise(bars):
        if confirmation.index != event.index + 1 or event.delta is None:
            continue
        span = event.high - event.low
        body = abs(event.close - event.open)
        if span <= 0 or body / span < Decimal("0.60"):
            continue
        if (
            event.close > event.open
            and event.delta > 0
            and (event.high - event.close) / span <= Decimal("0.15")
            and confirmation.low >= event.close - Decimal("0.50") * body
        ):
            return True
        if (
            event.close < event.open
            and event.delta < 0
            and (event.close - event.low) / span <= Decimal("0.15")
            and confirmation.high <= event.close + Decimal("0.50") * body
        ):
            return True
    return False


def _secado(
    bars: tuple[_FlowBar, ...],
    thresholds: OrderFlowThresholds,
    delta_threshold: Decimal | None,
) -> bool | None:
    if delta_threshold is None:
        return None
    if len(bars) < 2 or any(bar.delta is None for bar in bars):
        return None
    for index, event in enumerate(bars[:-1]):
        second = bars[index + 1]
        if (
            second.index != event.index + 1
            or event.delta is None
            or second.delta is None
        ):
            continue
        progress_limit = max(
            Decimal(2) * thresholds.tick_size,
            Decimal("0.15") * thresholds.atr_30s,
        )
        if (
            abs(event.delta) < delta_threshold
            or abs(event.close - event.open) > progress_limit
            or abs(second.delta) > Decimal("0.65") * abs(event.delta)
            or second.volume > Decimal("0.75") * event.volume
        ):
            continue
        reclaim_bars = bars[index + 1 : index + 3]
        if event.delta < 0 and any(
            bar.high >= event.point_of_control for bar in reclaim_bars
        ):
            return True
        if event.delta > 0 and any(
            bar.low <= event.point_of_control for bar in reclaim_bars
        ):
            return True
    return False


def _ceros(
    trades: tuple[TradePrint, ...], thresholds: OrderFlowThresholds
) -> bool | None:
    if not trades or any(trade.side is None for trade in trades):
        return None
    levels: dict[Decimal, dict[AggressorSide, Decimal]] = {}
    for trade in trades:
        price = cast(Decimal, trade.price)
        side = cast(AggressorSide, trade.side)
        sides = levels.setdefault(
            price, {AggressorSide.BUY: Decimal(0), AggressorSide.SELL: Decimal(0)}
        )
        sides[side] += cast(Decimal, trade.notional)
    minimum = min(levels)
    maximum = max(levels)
    span = maximum - minimum
    near_zero = thresholds.ceros_near_zero_notional
    large = thresholds.ceros_large_notional
    qualifying: list[Decimal] = []
    for price, sides in levels.items():
        smaller = min(sides.values())
        larger = max(sides.values())
        imbalance = larger / smaller if smaller > 0 else Decimal("Infinity")
        outer = (
            price <= minimum + Decimal("0.30") * span
            or price >= maximum - Decimal("0.30") * span
        )
        if (
            near_zero is not None
            and large is not None
            and smaller <= near_zero
            and larger >= large
            and imbalance >= Decimal(4)
            and outer
        ):
            qualifying.append(price)
    ordered = tuple(sorted(qualifying))
    return any(
        current - previous == thresholds.tick_size
        for previous, current in pairwise(ordered)
    )


def _unavailable() -> OrderFlowFacts:
    return OrderFlowFacts(
        state=EvidenceState.UNAVAILABLE,
        trade_count=0,
        unknown_aggressor_count=0,
        buy_notional=Decimal(0),
        sell_notional=Decimal(0),
        delta_notional=None,
        big_trades=(),
        reversal_mig=None,
        continuation_mig=None,
        secado=None,
        ceros=None,
        telemetry_only=True,
    )


def _require_utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def blocking_big_trade_ahead(
    facts: OrderFlowFacts,
    *,
    side: Side,
    reference_price: Decimal,
) -> bool:
    """Report an opposing Big Trade standing in the direction of travel.

    The specification forbids entering against a Big Trade and treats one that
    appears ahead of an open position as an exit signal. Ahead means above the
    reference price for a long and below it for a short, and opposing means the
    aggressor pushed against the traded direction.
    """
    if type(cast(object, facts)) is not OrderFlowFacts:
        raise TypeError("facts must be exact OrderFlowFacts")
    if type(side) is not Side:
        raise TypeError("side must be an exact Side")
    price = require_decimal(reference_price)
    if price <= 0:
        raise ValueError("reference_price must be positive")
    if facts.big_trades is None:
        # The caller has to decide what a window that cannot see means, and
        # answering False here would clear the path on its behalf.
        raise BigTradesUnmeasured("this window held too few events to rank")
    opposing = AggressorSide.SELL if side is Side.BUY else AggressorSide.BUY
    return any(
        cluster.side is opposing
        and (
            cluster.high_price >= price
            if side is Side.BUY
            else cluster.low_price <= price
        )
        for cluster in facts.big_trades
    )


__all__ = (
    "BIG_TRADE_EVENTS_PER_LIQUIDITY_SESSION",
    "BIG_TRADE_EXTREME_QUANTILE",
    "BIG_TRADE_NORMAL_QUANTILE",
    "BIG_TRADE_WINDOW",
    "DELTA_QUANTILE",
    "LIQUIDITY_SESSION",
    "MAXIMUM_BIG_TRADE_MARKERS",
    "MINIMUM_BIG_TRADE_EVENTS",
    "MINIMUM_DELTA_BARS",
    "THIRTY_SECOND_ATR_WINDOW",
    "AggressorSide",
    "BigTradeClass",
    "BigTradeCluster",
    "BigTradesUnmeasured",
    "OrderFlowFacts",
    "OrderFlowThresholds",
    "TradePrint",
    "aggregate_order_flow",
    "big_trade_quantile",
    "blocking_big_trade_ahead",
    "thirty_second_atr",
)
