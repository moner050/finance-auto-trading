from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from itertools import pairwise
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar

_FIVE_MINUTES = timedelta(minutes=5)
_MINIMUM_DISTINCT_TOUCHES = 3


@dataclass(frozen=True, slots=True)
class ZoneConfig:
    at_observed_all_time_high: bool
    source_timezone: str

    def __post_init__(self) -> None:
        if type(self.at_observed_all_time_high) is not bool:
            raise TypeError("at_observed_all_time_high must be bool")
        _zone_info(self.source_timezone)


@dataclass(frozen=True, slots=True)
class HlitZone:
    lower_boundary: Decimal
    upper_boundary: Decimal
    touch_count: int
    strength: int
    touched_at: tuple[datetime, ...]

    def __post_init__(self) -> None:
        if (
            type(self.lower_boundary) is not Decimal
            or type(self.upper_boundary) is not Decimal
            or not self.lower_boundary.is_finite()
            or not self.upper_boundary.is_finite()
            or self.lower_boundary > self.upper_boundary
        ):
            raise ValueError("zone boundaries must be ordered finite Decimals")
        if type(self.touch_count) is not int or self.touch_count < 1:
            raise ValueError("touch_count must be positive")
        if self.strength != min(self.touch_count, 5):
            raise ValueError("zone strength must cap distinct touches at five")
        if (
            type(self.touched_at) is not tuple
            or len(self.touched_at) != self.touch_count
            or any(later <= earlier for earlier, later in pairwise(self.touched_at))
        ):
            raise ValueError("zone touches must be unique ascending timestamps")


@dataclass(frozen=True, slots=True)
class ZoneFacts:
    observed_at: datetime
    source_timezone: str
    selected_dates: tuple[date, ...]
    bin_count: int
    zones: tuple[HlitZone, ...]

    def __post_init__(self) -> None:
        _zone_info(self.source_timezone)
        if type(self.selected_dates) is not tuple or any(
            type(value) is not date for value in self.selected_dates
        ):
            raise TypeError("selected_dates must be exact dates")
        if type(self.bin_count) is not int or self.bin_count < 1:
            raise ValueError("bin_count must be positive")
        if type(self.zones) is not tuple or any(
            type(zone) is not HlitZone for zone in self.zones
        ):
            raise TypeError("zones must contain exact HlitZone values")


def build_hlit_zones(
    bars: Sequence[CompletedOhlcvBar], config: ZoneConfig
) -> ZoneFacts:
    if type(cast(object, config)) is not ZoneConfig:
        raise TypeError("config must be exact ZoneConfig")
    values = tuple(bars)
    if not values or any(type(bar) is not CompletedOhlcvBar for bar in values):
        raise ValueError("zones require completed OHLCV bars")
    if any(later.timestamp <= earlier.timestamp for earlier, later in pairwise(values)):
        raise ValueError("zone bars must be strictly ascending")
    source_zone = _zone_info(config.source_timezone)
    by_date: dict[date, list[CompletedOhlcvBar]] = {}
    for bar in values:
        by_date.setdefault(bar.timestamp.astimezone(source_zone).date(), []).append(bar)
    for daily_bars in by_date.values():
        if any(
            later.timestamp - earlier.timestamp != _FIVE_MINUTES
            for earlier, later in pairwise(daily_bars)
        ):
            raise ValueError("zone evidence must be contiguous five-minute bars")
    required_dates = 3 if config.at_observed_all_time_high else 10
    ordered_dates = tuple(by_date)
    selected_dates = ordered_dates[-required_dates:]
    selected = tuple(bar for day in selected_dates for bar in by_date[day])
    observations = tuple(
        value for bar in selected for value in (bar.open, bar.high, bar.low, bar.close)
    )
    bin_count = _cube_root_ceiling(len(observations))
    if len(ordered_dates) < required_dates or min(observations) == max(observations):
        return ZoneFacts(
            observed_at=values[-1].timestamp,
            source_timezone=config.source_timezone,
            selected_dates=selected_dates,
            bin_count=bin_count,
            zones=(),
        )
    minimum = min(observations)
    span = _fraction(max(observations)) - _fraction(minimum)
    bins: list[dict[datetime, list[Decimal]]] = [dict() for _ in range(bin_count)]
    for bar in selected:
        for observation in (bar.open, bar.high, bar.low, bar.close):
            index = _bin_index(
                value=observation,
                minimum=_fraction(minimum),
                span=span,
                bin_count=bin_count,
            )
            bins[index].setdefault(bar.timestamp, []).append(observation)
    zones = tuple(
        HlitZone(
            lower_boundary=min(
                observation
                for values_by_bar in touches.values()
                for observation in values_by_bar
            ),
            upper_boundary=max(
                observation
                for values_by_bar in touches.values()
                for observation in values_by_bar
            ),
            touch_count=len(touches),
            strength=min(len(touches), 5),
            touched_at=tuple(sorted(touches)),
        )
        for touches in bins
        if len(touches) >= _MINIMUM_DISTINCT_TOUCHES
    )
    return ZoneFacts(
        observed_at=values[-1].timestamp,
        source_timezone=config.source_timezone,
        selected_dates=selected_dates,
        bin_count=bin_count,
        zones=zones,
    )


def _zone_info(value: object) -> ZoneInfo:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError("source_timezone must be an IANA timezone")
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError("source_timezone must be an IANA timezone") from error


def _fraction(value: Decimal) -> Fraction:
    numerator, denominator = value.as_integer_ratio()
    return Fraction(numerator, denominator)


def _bin_index(
    *, value: Decimal, minimum: Fraction, span: Fraction, bin_count: int
) -> int:
    offset = _fraction(value) - minimum
    index = int(offset * bin_count // span)
    return min(index, bin_count - 1)


def _cube_root_ceiling(value: int) -> int:
    candidate = 1
    while candidate**3 < value:
        candidate += 1
    return candidate


__all__ = ("HlitZone", "ZoneConfig", "ZoneFacts", "build_hlit_zones")
