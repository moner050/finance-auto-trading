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

# Section 10: "제가 필요한 날짜까지입니다" - as far back as is needed, and at an
# all-time high that is not far, because there is nothing marked above.
ALL_TIME_HIGH_DATES = 3
ORDINARY_DATES = 10

# What a caller has to fetch for zones to be obtainable at all. Ten dates of
# five-minute bars is more than one kline request returns, and reading the
# per-request ceiling as the venue's limit is what left this empty on every
# pass for the life of the system.
ZONE_HISTORY = timedelta(days=ORDINARY_DATES + 1)


@dataclass(frozen=True, slots=True)
class ZoneConfig:
    """What the caller knows that the bars do not say.

    The lookback is deliberately absent. Section 10 writes it as
    `3 if at_all_time_high(bars_5m) else 10` - a function of the same bars,
    not something handed in - and the version of this that took it as an
    input spent the system's whole life defaulted to False, which nobody
    noticed because zones were empty for a different reason at the same time.
    """

    source_timezone: str

    def __post_init__(self) -> None:
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


def at_observed_all_time_high(bars: Sequence[CompletedOhlcvBar]) -> bool:
    """Whether the last bar closed at the high of everything we can see.

    Observed, and the word is load-bearing: this is a statement about the
    window in hand, not about the instrument's history, and it cannot be
    anything else because the window is all the evidence there is.

    The test is exact rather than tolerant. A band - "within some fraction of
    the high" - is not in section 10, and a tolerance invented here would
    decide whether the lookback is three dates or ten, which decides whether
    zones exist. That is too much to hang on a number nobody published.
    """
    values = tuple(bars)
    if not values:
        return False
    return values[-1].close >= max(bar.high for bar in values)


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
    ordered_dates = tuple(by_date)
    required_dates = (
        ALL_TIME_HIGH_DATES if at_observed_all_time_high(values) else ORDINARY_DATES
    )
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


__all__ = (
    "ALL_TIME_HIGH_DATES",
    "ORDINARY_DATES",
    "ZONE_HISTORY",
    "HlitZone",
    "ZoneConfig",
    "ZoneFacts",
    "at_observed_all_time_high",
    "build_hlit_zones",
)
