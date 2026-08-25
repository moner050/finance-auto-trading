from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from autotrader.integrations.brokers.common import BrokerMarket
from autotrader.integrations.brokers.toss.adapter import (
    TossCandleInterval,
    TossCandlePage,
    TossCandleRecord,
)
from autotrader.integrations.market_data.toss_completed_candles import (
    compile_completed_toss_krx_ohlcv_bars,
)


def candle(
    timestamp: str,
    *,
    open_price: str = "100",
    close: str = "100",
    currency: str = "KRW",
) -> TossCandleRecord:
    return TossCandleRecord(
        timestamp=datetime.fromisoformat(timestamp),
        open_price=Decimal(open_price),
        high_price=Decimal("102"),
        low_price=Decimal("99"),
        close_price=Decimal(close),
        volume=Decimal("7"),
        currency=currency,
    )


def test_compiler_deduplicates_equal_inclusive_boundary_and_drops_current_minute() -> (
    None
):
    first = candle("2026-08-11T09:31:00+09:00", close="101")
    boundary = candle("2026-08-11T09:30:00+09:00", close="100")
    pages = (
        TossCandlePage(
            records=(first, boundary), next_before="2026-08-11T09:30:00+09:00"
        ),
        TossCandlePage(records=(boundary,), next_before=None),
    )

    bars = compile_completed_toss_krx_ohlcv_bars(
        market=BrokerMarket.KRX_STOCK,
        pages=pages,
        interval=TossCandleInterval.ONE_MINUTE,
        observed_at=datetime(2026, 8, 11, 0, 31, tzinfo=UTC),
    )

    assert [bar.timestamp for bar in bars] == [
        datetime(2026, 8, 11, 0, 31, tzinfo=UTC),
    ]
    assert bars[0].close == Decimal("100")


def test_ohlcv_compiler_preserves_open_and_drops_current_minute() -> None:
    completed = candle("2026-08-11T09:30:00+09:00", open_price="99", close="100")
    current = candle("2026-08-11T09:31:00+09:00", open_price="100", close="101")

    bars = compile_completed_toss_krx_ohlcv_bars(
        market=BrokerMarket.KRX_STOCK,
        pages=(TossCandlePage(records=(current, completed), next_before=None),),
        interval=TossCandleInterval.ONE_MINUTE,
        observed_at=datetime(2026, 8, 11, 0, 31, tzinfo=UTC),
    )

    assert [(bar.timestamp, bar.open, bar.close) for bar in bars] == [
        (datetime(2026, 8, 11, 0, 31, tzinfo=UTC), Decimal("99"), Decimal("100"))
    ]


def test_ohlcv_compiler_rejects_provider_correction_conflicts() -> None:
    started_at = "2026-08-11T09:30:00+09:00"
    original = candle(started_at, open_price="99", close="100")
    corrected = candle(started_at, open_price="101", close="100")

    with pytest.raises(ValueError, match="correction"):
        compile_completed_toss_krx_ohlcv_bars(
            market=BrokerMarket.KRX_STOCK,
            pages=(
                TossCandlePage(records=(original,), next_before=started_at),
                TossCandlePage(records=(corrected,), next_before=None),
            ),
            interval=TossCandleInterval.ONE_MINUTE,
            observed_at=datetime(2026, 8, 11, 0, 31, tzinfo=UTC),
        )


def test_compiler_delays_daily_bar_until_one_calendar_day_after_start() -> None:
    page = TossCandlePage(
        records=(candle("2026-08-10T09:00:00+09:00", close="100"),),
        next_before=None,
    )

    assert (
        compile_completed_toss_krx_ohlcv_bars(
            market=BrokerMarket.KRX_STOCK,
            pages=(page,),
            interval=TossCandleInterval.ONE_DAY,
            observed_at=datetime(2026, 8, 10, 23, 59, tzinfo=UTC),
        )
        == ()
    )
    bars = compile_completed_toss_krx_ohlcv_bars(
        market=BrokerMarket.KRX_STOCK,
        pages=(page,),
        interval=TossCandleInterval.ONE_DAY,
        observed_at=datetime(2026, 8, 11, 0, 0, tzinfo=UTC),
    )

    assert len(bars) == 1
    assert bars[0].timestamp == datetime(2026, 8, 11, 0, 0, tzinfo=UTC)


def test_compiler_rejects_conflicts_currency_market_and_non_utc_observation() -> None:
    started_at = "2026-08-10T09:00:00+09:00"
    original = candle(started_at, close="100")
    corrected = candle(started_at, close="101")
    conflict = (
        TossCandlePage(records=(original,), next_before=started_at),
        TossCandlePage(records=(corrected,), next_before=None),
    )
    observed_at = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="correction"):
        compile_completed_toss_krx_ohlcv_bars(
            market=BrokerMarket.KRX_STOCK,
            pages=conflict,
            interval=TossCandleInterval.ONE_DAY,
            observed_at=observed_at,
        )
    with pytest.raises(ValueError, match="KRW"):
        compile_completed_toss_krx_ohlcv_bars(
            market=BrokerMarket.KRX_STOCK,
            pages=(
                TossCandlePage(
                    records=(candle(started_at, currency="USD"),), next_before=None
                ),
            ),
            interval=TossCandleInterval.ONE_DAY,
            observed_at=observed_at,
        )
    with pytest.raises(ValueError, match="KRX_STOCK"):
        compile_completed_toss_krx_ohlcv_bars(
            market=BrokerMarket.US_STOCK,
            pages=(TossCandlePage(records=(original,), next_before=None),),
            interval=TossCandleInterval.ONE_DAY,
            observed_at=observed_at,
        )
    with pytest.raises(ValueError, match="UTC"):
        compile_completed_toss_krx_ohlcv_bars(
            market=BrokerMarket.KRX_STOCK,
            pages=(TossCandlePage(records=(original,), next_before=None),),
            interval=TossCandleInterval.ONE_DAY,
            observed_at=datetime(2026, 8, 11, 0, 0),
        )


@pytest.mark.parametrize(
    ("pages", "interval"),
    [
        ([], TossCandleInterval.ONE_MINUTE),
        ((object(),), TossCandleInterval.ONE_MINUTE),
        ((), "1m"),
    ],
)
def test_compiler_rejects_mutable_or_invalid_provider_inputs(
    pages: object, interval: object
) -> None:
    with pytest.raises(ValueError):
        compile_completed_toss_krx_ohlcv_bars(
            market=BrokerMarket.KRX_STOCK,
            pages=pages,
            interval=interval,
            observed_at=datetime(2026, 8, 11, 0, 0, tzinfo=UTC),
        )
