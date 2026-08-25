from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import cast
from zoneinfo import ZoneInfo

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar

_KST = ZoneInfo("Asia/Seoul")
_CONSECUTIVE_MINUTES_ERROR = (
    "KRX HLIT timeframe bars require consecutive completed minutes"
)
_SUPPORTED_INTERVAL_ERROR = "KRX HLIT timeframe requires a supported interval"


class HlitTimeframe(StrEnum):
    ONE_MINUTE = "1m"
    FIVE_MINUTES = "5m"
    FIFTEEN_MINUTES = "15m"
    ONE_HOUR = "1h"


def compile_completed_krx_hlit_timeframe(
    *, bars: object, timeframe: object
) -> tuple[CompletedOhlcvBar, ...]:
    """Compile only complete KST-clock higher-timeframe bars from completed minutes."""
    interval_minutes = _interval_minutes(timeframe)
    source_bars = _completed_minutes(bars)
    _require_consecutive_minutes(source_bars)

    buckets: dict[tuple[date, datetime], list[CompletedOhlcvBar]] = {}
    for bar in source_bars:
        local_timestamp = bar.timestamp.astimezone(_KST)
        bucket_end = _bucket_end(local_timestamp, interval_minutes)
        buckets.setdefault((local_timestamp.date(), bucket_end), []).append(bar)

    result: list[CompletedOhlcvBar] = []
    for bucket in buckets.values():
        first = bucket[0].timestamp.astimezone(_KST)
        last = bucket[-1].timestamp.astimezone(_KST)
        bucket_end = _bucket_end(last, interval_minutes)
        if (
            len(bucket) != interval_minutes
            or first != bucket_end - timedelta(minutes=interval_minutes - 1)
            or last != bucket_end
        ):
            continue
        result.append(
            CompletedOhlcvBar(
                timestamp=bucket_end.astimezone(UTC),
                open=bucket[0].open,
                high=max(bar.high for bar in bucket),
                low=min(bar.low for bar in bucket),
                close=bucket[-1].close,
                volume=sum((bar.volume for bar in bucket), start=Decimal()),
            )
        )
    return tuple(result)


def _interval_minutes(value: object) -> int:
    if value is HlitTimeframe.ONE_MINUTE:
        return 1
    if value is HlitTimeframe.FIVE_MINUTES:
        return 5
    if value is HlitTimeframe.FIFTEEN_MINUTES:
        return 15
    if value is HlitTimeframe.ONE_HOUR:
        return 60
    raise ValueError(_SUPPORTED_INTERVAL_ERROR)


def _completed_minutes(value: object) -> tuple[CompletedOhlcvBar, ...]:
    if type(value) is not tuple:
        raise ValueError(_CONSECUTIVE_MINUTES_ERROR)
    bars = cast(tuple[object, ...], value)
    if not all(isinstance(bar, CompletedOhlcvBar) for bar in bars):
        raise ValueError(_CONSECUTIVE_MINUTES_ERROR)
    return cast(tuple[CompletedOhlcvBar, ...], bars)


def _require_consecutive_minutes(bars: tuple[CompletedOhlcvBar, ...]) -> None:
    previous = None
    for bar in bars:
        timestamp = bar.timestamp
        if (
            type(timestamp) is not datetime
            or timestamp.tzinfo is not UTC
            or timestamp.utcoffset() != UTC.utcoffset(timestamp)
            or timestamp.second
            or timestamp.microsecond
        ):
            raise ValueError(_CONSECUTIVE_MINUTES_ERROR)
        if previous is not None and (
            timestamp <= previous or timestamp - previous != timedelta(minutes=1)
        ):
            raise ValueError(_CONSECUTIVE_MINUTES_ERROR)
        previous = timestamp


def _bucket_end(timestamp: datetime, interval_minutes: int) -> datetime:
    minute = timestamp.minute
    bucket_minute = (
        (minute + interval_minutes - 1) // interval_minutes
    ) * interval_minutes
    if bucket_minute == 60:
        return timestamp.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return timestamp.replace(
        minute=bucket_minute,
        second=0,
        microsecond=0,
    )
