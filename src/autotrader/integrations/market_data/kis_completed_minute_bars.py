from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.integrations.brokers.kis.domestic_stock_contracts import (
    KisDomesticMarket,
    KisDomesticMinuteChartPage,
    KisDomesticMinuteRecord,
)

_KST = ZoneInfo("Asia/Seoul")


def compile_completed_kis_krx_minute_ohlcv_bars(
    *, market: object, pages: object, observed_at: datetime
) -> tuple[CompletedOhlcvBar, ...]:
    """Compile completed KIS KRX minute candles into provider-neutral OHLCV bars."""
    if market is not KisDomesticMarket.KRX:
        raise ValueError("KIS completed minute bars require KRX")
    observed_at = _require_utc(observed_at)
    records_by_label: dict[datetime, KisDomesticMinuteRecord] = {}
    for page in _pages(pages):
        for record in page.records:
            label = datetime.combine(
                record.trading_date, record.trading_time, tzinfo=_KST
            )
            previous = records_by_label.get(label)
            if previous is not None and previous != record:
                raise ValueError("KIS minute candle correction conflict")
            records_by_label[label] = record
    return tuple(
        CompletedOhlcvBar(
            timestamp=completion,
            open=record.open_price,
            high=record.high_price,
            low=record.low_price,
            close=record.close_price,
            volume=record.volume,
        )
        for label, record in sorted(records_by_label.items())
        if (completion := (label + timedelta(minutes=1)).astimezone(UTC)) <= observed_at
    )


def _require_utc(value: object) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is not UTC
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValueError("KIS completed minute bars require UTC observed_at")
    return value


def _pages(value: object) -> tuple[KisDomesticMinuteChartPage, ...]:
    pages = cast(tuple[object, ...], value)
    if type(value) is not tuple or not all(
        isinstance(page, KisDomesticMinuteChartPage) for page in pages
    ):
        raise ValueError("KIS completed minute bars require immutable minute pages")
    return cast(tuple[KisDomesticMinuteChartPage, ...], pages)
