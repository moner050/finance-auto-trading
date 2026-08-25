from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.integrations.brokers.common import BrokerMarket
from autotrader.integrations.brokers.toss.market_data_contracts import (
    TossCandleInterval,
    TossCandlePage,
    TossCandleRecord,
)


def compile_completed_toss_krx_ohlcv_bars(
    *, market: object, pages: object, interval: object, observed_at: datetime
) -> tuple[CompletedOhlcvBar, ...]:
    """Compile only completed, uncorrected KRW KRX Toss records into OHLCV bars."""
    if market is not BrokerMarket.KRX_STOCK:
        raise ValueError("Toss completed bars require KRX_STOCK")
    _require_utc(observed_at)
    duration = _interval_duration(interval)
    records_by_start: dict[datetime, TossCandleRecord] = {}
    for page in _pages(pages):
        for record in page.records:
            if record.currency != "KRW":
                raise ValueError("Toss KRX candles must use KRW")
            started_at = record.timestamp.astimezone(UTC)
            previous = records_by_start.get(started_at)
            if previous is not None and previous != record:
                raise ValueError("Toss candle correction conflict")
            records_by_start[started_at] = record
    return tuple(
        CompletedOhlcvBar(
            timestamp=started_at + duration,
            open=record.open_price,
            high=record.high_price,
            low=record.low_price,
            close=record.close_price,
            volume=record.volume,
        )
        for started_at, record in sorted(records_by_start.items())
        if started_at + duration <= observed_at
    )


def _require_utc(value: object) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is not UTC
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValueError("Toss completed bars require UTC observed_at")
    return value


def _interval_duration(value: object) -> timedelta:
    if value is TossCandleInterval.ONE_MINUTE:
        return timedelta(minutes=1)
    if value is TossCandleInterval.ONE_DAY:
        return timedelta(days=1)
    raise ValueError("Toss completed bars require a supported Toss candle interval")


def _pages(value: object) -> tuple[TossCandlePage, ...]:
    pages = cast(tuple[object, ...], value)
    if type(value) is not tuple or not all(
        isinstance(page, TossCandlePage) for page in pages
    ):
        raise ValueError("Toss completed bars require immutable Toss candle pages")
    return cast(tuple[TossCandlePage, ...], pages)
