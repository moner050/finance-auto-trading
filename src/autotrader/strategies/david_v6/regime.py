from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import cast

from autotrader.shared.decimal import require_decimal
from autotrader.strategies.david_v6.models import EvidenceState

_BOTTOM_QUINTILE = Decimal("0.20")
_UPPER_DECILE = Decimal("0.90")
_LOWER_DECILE = Decimal("0.10")


class RegimeLabel(StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    BALANCE = "BALANCE"


@dataclass(frozen=True, slots=True)
class PessimismInputs:
    completed_date: date | None
    volatility_percentile: Decimal | None
    put_call_percentile: Decimal | None
    breadth_percentile: Decimal | None

    def __post_init__(self) -> None:
        if self.completed_date is not None and type(self.completed_date) is not date:
            raise TypeError("completed_date must be an exact date")
        for name in (
            "volatility_percentile",
            "put_call_percentile",
            "breadth_percentile",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            percentile = require_decimal(value)
            if not Decimal(0) <= percentile <= Decimal(1):
                raise ValueError(f"{name} must be between zero and one")
            object.__setattr__(self, name, percentile)


@dataclass(frozen=True, slots=True)
class RegimeFacts:
    """The author's regime, and two observations beside it.

    Section 2.1 gives the regime as SMA 6/70/200 on the instrument: slope of
    the 200 positive, the 70 above it, the 70's slope positive. That is all of
    it, and `trend` is it.

    `sideways` and `low_volatility` are not in that rule. They were added on
    top of it and they used to exclude a trade on their own, which made the
    system refuse in conditions the author traded through. They are reported
    now and gate nothing.

    `pessimism_extreme` is a condition on one signal, not on the regime. The
    author uses it for a MACD cross below zero and nowhere else, so its
    absence leaves the regime available rather than blocking every decision.
    """

    state: EvidenceState
    trend: RegimeLabel | None
    sideways: bool | None
    low_volatility: bool | None
    pessimism_extreme: bool | None
    excluded: bool


def daily_returns(closes: Sequence[Decimal]) -> tuple[Decimal, ...]:
    """The series `evaluate_regime` ranks a trend from, from daily closes.

    Section 2.1 gives the author's regime as SMA 6/70/200 on the instrument
    itself: slope(sma200) > 0 and sma70 > sma200 and slope(sma70) > 0, with
    those lengths marked as observed from his screen and not to be optimised.
    `_trend` implements exactly that, over a level series it rebuilds from
    returns.

    Rebasing is a positive scale factor and a simple moving average is linear,
    so the comparisons and the slopes come out the same whether they are taken
    over closes or over closes divided by the first one. The instrument's own
    returns therefore reproduce the author's rule rather than approximate it,
    and no separate benchmark has to be chosen.
    """
    prices = [require_decimal(close) for close in closes]
    if any(price <= 0 for price in prices):
        raise ValueError("a close must be positive")
    return tuple((later - earlier) / earlier for earlier, later in pairwise(prices))


def evaluate_regime(
    *,
    benchmark_returns: Sequence[Decimal],
    atr_ratio: Decimal | None = None,
    range_efficiency: Decimal | None = None,
    pessimism_inputs: PessimismInputs | None = None,
) -> RegimeFacts:
    """The regime, from the only rule the author gave for it.

    Everything except the trend is optional because everything except the
    trend is something else: two observations the author's rule does not
    consult, and a condition that belongs to one signal.
    """
    volatility_percentile = (
        None if atr_ratio is None else _percentile(atr_ratio, "atr_ratio")
    )
    efficiency_percentile = (
        None
        if range_efficiency is None
        else _percentile(range_efficiency, "range_efficiency")
    )
    if pessimism_inputs is not None and (
        type(cast(object, pessimism_inputs)) is not PessimismInputs
    ):
        raise TypeError("pessimism_inputs must be exact PessimismInputs")
    returns = _returns(benchmark_returns)
    sideways = (
        None
        if efficiency_percentile is None
        else efficiency_percentile <= _BOTTOM_QUINTILE
    )
    low_volatility = (
        None
        if volatility_percentile is None
        else volatility_percentile <= _BOTTOM_QUINTILE
    )
    pessimism_extreme = (
        None if pessimism_inputs is None else _pessimism_extreme(pessimism_inputs)
    )
    trend = _trend(returns)
    # The regime is available when its rule can be evaluated, which needs the
    # two hundred closes the SMAs are taken over and nothing else.
    available = trend is not None
    return RegimeFacts(
        state=(EvidenceState.AVAILABLE if available else EvidenceState.UNAVAILABLE),
        trend=trend,
        sideways=sideways,
        low_volatility=low_volatility,
        pessimism_extreme=pessimism_extreme,
        excluded=not available,
    )


def _returns(values: Sequence[Decimal]) -> tuple[Decimal, ...]:
    result: list[Decimal] = []
    for value in values:
        daily_return = require_decimal(value)
        if daily_return <= Decimal("-1"):
            raise ValueError("benchmark return must be greater than negative one")
        result.append(daily_return)
    return tuple(result)


def _trend(returns: tuple[Decimal, ...]) -> RegimeLabel | None:
    if len(returns) < 200:
        return None
    level = Decimal(1)
    levels = [level]
    for daily_return in returns:
        level *= Decimal(1) + daily_return
        levels.append(level)
    sma_200 = sum(levels[-200:], start=Decimal(0)) / Decimal(200)
    previous_sma_200 = sum(levels[-201:-1], start=Decimal(0)) / Decimal(200)
    sma_70 = sum(levels[-70:], start=Decimal(0)) / Decimal(70)
    previous_sma_70 = sum(levels[-71:-1], start=Decimal(0)) / Decimal(70)
    if sma_200 > previous_sma_200 and sma_70 > sma_200 and sma_70 > previous_sma_70:
        return RegimeLabel.TREND_UP
    if sma_200 < previous_sma_200 and sma_70 < sma_200 and sma_70 < previous_sma_70:
        return RegimeLabel.TREND_DOWN
    return RegimeLabel.BALANCE


def _pessimism_extreme(inputs: PessimismInputs) -> bool | None:
    """Whether the market is at a pessimism extreme, from what was measured.

    Section 2.3 splits this in two. The quantitative triple - put-call ratio,
    a VIX percentile and breadth - is marked "커리큘럼 M3", the curriculum. The
    detector marked "(A0) 실제로 사용", what the author actually used, is
    `media_panic_signal`, which is qualitative and which this system cannot
    read and must not invent.

    So the composite here is a stand-in, and it is treated as one. Requiring
    all three of a curriculum triple made a signal wait on the one percentile
    with no history to rank against, which is a stricter condition than the
    author ever applied - he read a newspaper.

    Two things this deliberately keeps. The threshold does not soften: an
    extreme still needs two agreeing components, so with two measured they
    must both agree, and one component alone is never enough to call an
    extreme. And what is measured is still ranked - a missing percentile is
    skipped, never filled, because a filled one would be counted in every
    rank thereafter.

    The volatility component is the instrument's own realised volatility. The
    author's second component is a VIX percentile, so this is a substitution
    as well, and it is named here rather than hidden behind the word
    "volatility".
    """
    if inputs.completed_date is None:
        return None
    matches = [
        percentile >= _UPPER_DECILE
        for percentile in (inputs.volatility_percentile, inputs.put_call_percentile)
        if percentile is not None
    ]
    if inputs.breadth_percentile is not None:
        matches.append(inputs.breadth_percentile <= _LOWER_DECILE)
    # Fewer than two measured components cannot produce two that agree, so
    # there is nothing to judge rather than an extreme that is absent.
    if len(matches) < 2:
        return None
    return sum(matches) >= 2


def _percentile(value: object, name: str) -> Decimal:
    percentile = require_decimal(value)
    if not Decimal(0) <= percentile <= Decimal(1):
        raise ValueError(f"{name} must be a point-in-time percentile")
    return percentile


__all__ = (
    "PessimismInputs",
    "RegimeFacts",
    "RegimeLabel",
    "daily_returns",
    "evaluate_regime",
)
