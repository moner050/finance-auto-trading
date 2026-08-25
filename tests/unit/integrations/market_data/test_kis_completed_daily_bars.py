from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from autotrader.integrations.brokers.kis.domestic_stock import (
    KisDomesticDailyChartPage,
    KisDomesticDailyRecord,
    KisDomesticMarket,
)
from autotrader.integrations.market_data.kis_completed_daily_bars import (
    compile_completed_kis_krx_daily_ohlcv_bars,
)


def daily_record(*, close: str = "102") -> KisDomesticDailyRecord:
    return KisDomesticDailyRecord(
        trading_date=date(2026, 8, 10),
        open_price=Decimal("99"),
        high_price=Decimal("103"),
        low_price=Decimal("98"),
        close_price=Decimal(close),
        volume=Decimal("1200"),
    )


def test_compiler_emits_a_daily_bar_at_krx_close_in_utc() -> None:
    bars = compile_completed_kis_krx_daily_ohlcv_bars(
        market=KisDomesticMarket.KRX,
        pages=(KisDomesticDailyChartPage(records=(daily_record(),)),),
        observed_at=datetime(2026, 8, 10, 6, 30, tzinfo=UTC),
    )

    assert len(bars) == 1
    assert bars[0].timestamp == datetime(2026, 8, 10, 6, 30, tzinfo=UTC)


def test_ohlcv_compiler_retains_kis_daily_open_at_the_completed_close() -> None:
    bars = compile_completed_kis_krx_daily_ohlcv_bars(
        market=KisDomesticMarket.KRX,
        pages=(KisDomesticDailyChartPage(records=(daily_record(),)),),
        observed_at=datetime(2026, 8, 10, 6, 30, tzinfo=UTC),
    )

    assert len(bars) == 1
    assert bars[0].timestamp == datetime(2026, 8, 10, 6, 30, tzinfo=UTC)
    assert bars[0].open == Decimal("99")
    assert bars[0].close == Decimal("102")


def test_compiler_deduplicates_equal_records_and_sorts_completed_dates() -> None:
    august_ten = daily_record()
    august_nine = KisDomesticDailyRecord(
        trading_date=date(2026, 8, 9),
        open_price=Decimal("99"),
        high_price=Decimal("103"),
        low_price=Decimal("98"),
        close_price=Decimal("101"),
        volume=Decimal("1200"),
    )

    bars = compile_completed_kis_krx_daily_ohlcv_bars(
        market=KisDomesticMarket.KRX,
        pages=(
            KisDomesticDailyChartPage(records=(august_ten,)),
            KisDomesticDailyChartPage(records=(august_ten, august_nine)),
        ),
        observed_at=datetime(2026, 8, 10, 6, 30, tzinfo=UTC),
    )

    assert [bar.timestamp for bar in bars] == [
        datetime(2026, 8, 9, 6, 30, tzinfo=UTC),
        datetime(2026, 8, 10, 6, 30, tzinfo=UTC),
    ]


def test_compiler_rejects_corrections_and_uncompleted_daily_records() -> None:
    original = daily_record(close="102")
    corrected = daily_record(close="101")
    pages = (
        KisDomesticDailyChartPage(records=(original,)),
        KisDomesticDailyChartPage(records=(corrected,)),
    )

    with pytest.raises(ValueError, match="KIS daily candle correction conflict"):
        compile_completed_kis_krx_daily_ohlcv_bars(
            market=KisDomesticMarket.KRX,
            pages=pages,
            observed_at=datetime(2026, 8, 10, 6, 30, tzinfo=UTC),
        )
    assert (
        compile_completed_kis_krx_daily_ohlcv_bars(
            market=KisDomesticMarket.KRX,
            pages=(KisDomesticDailyChartPage(records=(original,)),),
            observed_at=datetime(2026, 8, 10, 6, 29, tzinfo=UTC),
        )
        == ()
    )


@pytest.mark.parametrize(
    "market, pages, observed_at",
    [
        (
            KisDomesticMarket.NXT,
            (KisDomesticDailyChartPage(records=(daily_record(),)),),
            datetime(2026, 8, 10, 6, 30, tzinfo=UTC),
        ),
        (
            KisDomesticMarket.KRX,
            [KisDomesticDailyChartPage(records=(daily_record(),))],
            datetime(2026, 8, 10, 6, 30, tzinfo=UTC),
        ),
        (
            KisDomesticMarket.KRX,
            (KisDomesticDailyChartPage(records=(daily_record(),)),),
            datetime(2026, 8, 10, 6, 30),
        ),
        (
            KisDomesticMarket.KRX,
            (KisDomesticDailyChartPage(records=(daily_record(),)),),
            datetime.fromisoformat("2026-08-10T15:30:00+09:00"),
        ),
    ],
)
def test_compiler_rejects_invalid_market_pages_or_observation(
    market: object, pages: object, observed_at: datetime
) -> None:
    with pytest.raises(ValueError):
        compile_completed_kis_krx_daily_ohlcv_bars(
            market=market, pages=pages, observed_at=observed_at
        )
