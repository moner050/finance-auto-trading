from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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


@dataclass(frozen=True, slots=True)
class OrderFlowThresholds:
    tick_size: Decimal
    normal_big_trade_notional: Decimal
    extreme_big_trade_notional: Decimal
    delta_p90_notional: Decimal
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
            "normal_big_trade_notional",
            "extreme_big_trade_notional",
            "delta_p90_notional",
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
        if self.extreme_big_trade_notional < self.normal_big_trade_notional:
            raise ValueError("extreme Big Trade threshold cannot be below normal")
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
    big_trades: tuple[BigTradeCluster, ...]
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
    return OrderFlowFacts(
        state=(EvidenceState.UNKNOWN if unknown_count else EvidenceState.AVAILABLE),
        trade_count=len(deduplicated),
        unknown_aggressor_count=unknown_count,
        buy_notional=buy_notional,
        sell_notional=sell_notional,
        delta_notional=(None if unknown_count else buy_notional - sell_notional),
        big_trades=_big_trades(known, thresholds),
        reversal_mig=_reversal_mig(bars, thresholds),
        continuation_mig=_continuation_mig(bars, thresholds),
        secado=_secado(bars, thresholds),
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


def _big_trades(
    trades: tuple[TradePrint, ...], thresholds: OrderFlowThresholds
) -> tuple[BigTradeCluster, ...]:
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
    clusters: list[BigTradeCluster] = []
    for group in groups:
        summed = sum(
            (cast(Decimal, trade.notional) for trade in group), start=Decimal(0)
        )
        if summed < thresholds.normal_big_trade_notional:
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
                    BigTradeClass.EXTREME
                    if summed >= thresholds.extreme_big_trade_notional
                    else BigTradeClass.NORMAL
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


def _reversal_mig(
    bars: tuple[_FlowBar, ...], thresholds: OrderFlowThresholds
) -> bool | None:
    if len(bars) < 2 or any(bar.delta is None for bar in bars):
        return None
    for event, confirmation in pairwise(bars):
        if confirmation.index != event.index + 1 or event.delta is None:
            continue
        span = event.high - event.low
        if span <= 0 or abs(event.delta) < thresholds.delta_p90_notional:
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
    bars: tuple[_FlowBar, ...], thresholds: OrderFlowThresholds
) -> bool | None:
    del thresholds
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


def _secado(bars: tuple[_FlowBar, ...], thresholds: OrderFlowThresholds) -> bool | None:
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
            abs(event.delta) < thresholds.delta_p90_notional
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
    "AggressorSide",
    "BigTradeClass",
    "BigTradeCluster",
    "OrderFlowFacts",
    "OrderFlowThresholds",
    "TradePrint",
    "aggregate_order_flow",
    "blocking_big_trade_ahead",
)
