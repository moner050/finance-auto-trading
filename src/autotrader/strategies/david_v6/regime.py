from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
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
    state: EvidenceState
    trend: RegimeLabel | None
    sideways: bool
    low_volatility: bool
    pessimism_extreme: bool | None
    excluded: bool


def evaluate_regime(
    *,
    benchmark_returns: Sequence[Decimal],
    atr_ratio: Decimal,
    range_efficiency: Decimal,
    pessimism_inputs: PessimismInputs,
) -> RegimeFacts:
    volatility_percentile = _percentile(atr_ratio, "atr_ratio")
    efficiency_percentile = _percentile(range_efficiency, "range_efficiency")
    if type(cast(object, pessimism_inputs)) is not PessimismInputs:
        raise TypeError("pessimism_inputs must be exact PessimismInputs")
    returns = _returns(benchmark_returns)
    sideways = efficiency_percentile <= _BOTTOM_QUINTILE
    low_volatility = volatility_percentile <= _BOTTOM_QUINTILE
    pessimism_extreme = _pessimism_extreme(pessimism_inputs)
    trend = _trend(returns)
    available = trend is not None and pessimism_extreme is not None
    return RegimeFacts(
        state=(EvidenceState.AVAILABLE if available else EvidenceState.UNAVAILABLE),
        trend=trend,
        sideways=sideways,
        low_volatility=low_volatility,
        pessimism_extreme=pessimism_extreme,
        excluded=sideways or low_volatility or not available,
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
    values = (
        inputs.volatility_percentile,
        inputs.put_call_percentile,
        inputs.breadth_percentile,
    )
    if inputs.completed_date is None or any(value is None for value in values):
        return None
    volatility, put_call, breadth = cast(tuple[Decimal, Decimal, Decimal], values)
    matches = (
        volatility >= _UPPER_DECILE,
        put_call >= _UPPER_DECILE,
        breadth <= _LOWER_DECILE,
    )
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
    "evaluate_regime",
)
