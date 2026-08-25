from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import cast
from zoneinfo import ZoneInfo

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.integrations.brokers.kis.domestic_stock import (
    KisDomesticDailyChartPage,
    KisDomesticDailyRecord,
    KisDomesticMarket,
)

_KST = ZoneInfo("Asia/Seoul")
_KRX_DAILY_CLOSE = time(15, 30)


def compile_completed_kis_krx_daily_ohlcv_bars(
    *, market: object, pages: object, observed_at: datetime
) -> tuple[CompletedOhlcvBar, ...]:
    """Compile completed KIS KRX daily candles retaining source OHLCV."""
    if market is not KisDomesticMarket.KRX:
        raise ValueError("KIS completed daily bars require KRX")
    observed_at = _require_utc(observed_at)
    records_by_date: dict[date, KisDomesticDailyRecord] = {}
    for page in _pages(pages):
        for record in page.records:
            previous = records_by_date.get(record.trading_date)
            if previous is not None and previous != record:
                raise ValueError("KIS daily candle correction conflict")
            records_by_date[record.trading_date] = record
    return tuple(
        CompletedOhlcvBar(
            timestamp=completion,
            open=record.open_price,
            high=record.high_price,
            low=record.low_price,
            close=record.close_price,
            volume=record.volume,
        )
        for trading_date, record in sorted(records_by_date.items())
        if (completion := _completion_at(trading_date)) <= observed_at
    )


def _require_utc(value: object) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is not UTC
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValueError("KIS completed daily bars require UTC observed_at")
    return value


def _pages(value: object) -> tuple[KisDomesticDailyChartPage, ...]:
    pages = cast(tuple[object, ...], value)
    if type(value) is not tuple or not all(
        isinstance(page, KisDomesticDailyChartPage) for page in pages
    ):
        raise ValueError("KIS completed daily bars require immutable daily pages")
    return cast(tuple[KisDomesticDailyChartPage, ...], pages)


def _completion_at(trading_date: date) -> datetime:
    return datetime.combine(trading_date, _KRX_DAILY_CLOSE, tzinfo=_KST).astimezone(UTC)
