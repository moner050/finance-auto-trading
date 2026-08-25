from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import cast

from autotrader.shared.decimal import require_decimal
from autotrader.strategies.david_v6.models import EvidenceState
from autotrader.strategies.david_v6.order_flow import AggressorSide, TradePrint

_VALUE_AREA_FRACTION = Decimal("0.70")


@dataclass(frozen=True, slots=True)
class ProfileLevel:
    price: Decimal
    buy_notional: Decimal
    sell_notional: Decimal
    unknown_notional: Decimal
    total_notional: Decimal
    delta_notional: Decimal
    imbalance_ratio: Decimal | None


@dataclass(frozen=True, slots=True)
class ProfileFacts:
    state: EvidenceState
    levels: tuple[ProfileLevel, ...]
    point_of_control: Decimal | None
    value_area_low: Decimal | None
    value_area_high: Decimal | None
    total_notional: Decimal


def build_profile(
    trades: Sequence[TradePrint],
    *,
    tick_size: Decimal,
) -> ProfileFacts:
    size = require_decimal(tick_size)
    if size <= 0:
        raise ValueError("tick_size must be positive")
    if any(type(trade) is not TradePrint for trade in trades):
        raise TypeError("trades must contain exact TradePrint values")
    deduplicated = _deduplicate(tuple(trades))
    if not deduplicated or any(trade.notional is None for trade in deduplicated):
        return _unavailable()

    buckets: dict[Decimal, dict[AggressorSide | None, Decimal]] = {}
    for trade in deduplicated:
        price = _round_to_bucket(cast(Decimal, trade.price), size)
        sides = buckets.setdefault(
            price,
            {
                AggressorSide.BUY: Decimal(0),
                AggressorSide.SELL: Decimal(0),
                None: Decimal(0),
            },
        )
        sides[trade.side] += cast(Decimal, trade.notional)

    levels = tuple(_level(price, sides) for price, sides in sorted(buckets.items()))
    total = sum((level.total_notional for level in levels), start=Decimal(0))
    current_price = _round_to_bucket(
        cast(Decimal, deduplicated[-1].price),
        size,
    )
    poc_index = min(
        range(len(levels)),
        key=lambda index: (
            -levels[index].total_notional,
            abs(levels[index].price - current_price),
            levels[index].price,
        ),
    )
    low_index, high_index = _value_area(levels, poc_index, current_price, total)
    return ProfileFacts(
        state=(
            EvidenceState.UNKNOWN
            if any(level.unknown_notional > 0 for level in levels)
            else EvidenceState.AVAILABLE
        ),
        levels=levels,
        point_of_control=levels[poc_index].price,
        value_area_low=levels[low_index].price,
        value_area_high=levels[high_index].price,
        total_notional=total,
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


def _round_to_bucket(price: Decimal, tick_size: Decimal) -> Decimal:
    units = (price / tick_size).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return units * tick_size


def _level(
    price: Decimal,
    sides: dict[AggressorSide | None, Decimal],
) -> ProfileLevel:
    buy = sides[AggressorSide.BUY]
    sell = sides[AggressorSide.SELL]
    unknown = sides[None]
    smaller = min(buy, sell)
    imbalance = max(buy, sell) / smaller if smaller > 0 else None
    return ProfileLevel(
        price=price,
        buy_notional=buy,
        sell_notional=sell,
        unknown_notional=unknown,
        total_notional=buy + sell + unknown,
        delta_notional=buy - sell,
        imbalance_ratio=imbalance,
    )


def _value_area(
    levels: tuple[ProfileLevel, ...],
    poc_index: int,
    current_price: Decimal,
    total_notional: Decimal,
) -> tuple[int, int]:
    low = high = poc_index
    included = levels[poc_index].total_notional
    target = _VALUE_AREA_FRACTION * total_notional
    while included < target and (low > 0 or high < len(levels) - 1):
        candidates = tuple(
            index for index in (low - 1, high + 1) if 0 <= index < len(levels)
        )
        selected = min(
            candidates,
            key=lambda index: (
                -levels[index].total_notional,
                abs(levels[index].price - current_price),
                levels[index].price,
            ),
        )
        included += levels[selected].total_notional
        low = min(low, selected)
        high = max(high, selected)
    return low, high


def _unavailable() -> ProfileFacts:
    return ProfileFacts(
        state=EvidenceState.UNAVAILABLE,
        levels=(),
        point_of_control=None,
        value_area_low=None,
        value_area_high=None,
        total_notional=Decimal(0),
    )


__all__ = ("ProfileFacts", "ProfileLevel", "build_profile")
