from __future__ import annotations

import ast
import subprocess
import sys
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

from autotrader.integrations.brokers.kis.domestic_stock import (
    KisDomesticMarket,
    KisDomesticMinuteChartPage,
    KisDomesticMinuteRecord,
)
from autotrader.integrations.market_data.kis_completed_minute_bars import (
    compile_completed_kis_krx_minute_ohlcv_bars,
)


def minute(
    trading_time: time, *, open_price: str = "99", close: str = "100"
) -> KisDomesticMinuteRecord:
    return KisDomesticMinuteRecord(
        trading_date=date(2026, 8, 11),
        trading_time=trading_time,
        open_price=Decimal(open_price),
        high_price=Decimal("103"),
        low_price=Decimal("98"),
        close_price=Decimal(close),
        volume=Decimal("1200"),
    )


def test_compiler_emits_only_completed_kst_minutes_as_utc_bars() -> None:
    first = minute(time(9, 0), close="100")
    active = minute(time(9, 1), close="101")

    bars = compile_completed_kis_krx_minute_ohlcv_bars(
        market=KisDomesticMarket.KRX,
        pages=(KisDomesticMinuteChartPage(records=(active, first)),),
        observed_at=datetime(2026, 8, 11, 0, 1, tzinfo=UTC),
    )

    assert [(bar.timestamp, bar.close) for bar in bars] == [
        (datetime(2026, 8, 11, 0, 1, tzinfo=UTC), Decimal("100")),
    ]


def test_ohlcv_compiler_preserves_open_and_excludes_current_minute() -> None:
    completed = minute(time(9, 0), open_price="99", close="100")
    current = minute(time(9, 1), open_price="100", close="101")

    bars = compile_completed_kis_krx_minute_ohlcv_bars(
        market=KisDomesticMarket.KRX,
        pages=(KisDomesticMinuteChartPage(records=(current, completed)),),
        observed_at=datetime(2026, 8, 11, 0, 1, tzinfo=UTC),
    )

    assert [(bar.timestamp, bar.open, bar.close) for bar in bars] == [
        (datetime(2026, 8, 11, 0, 1, tzinfo=UTC), Decimal("99"), Decimal("100"))
    ]


def test_ohlcv_compiler_rejects_provider_correction_conflicts() -> None:
    original = minute(time(9, 0), open_price="99", close="100")
    corrected = minute(time(9, 0), open_price="98", close="100")

    with pytest.raises(ValueError, match="KIS minute candle correction conflict"):
        compile_completed_kis_krx_minute_ohlcv_bars(
            market=KisDomesticMarket.KRX,
            pages=(
                KisDomesticMinuteChartPage(records=(original,)),
                KisDomesticMinuteChartPage(records=(corrected,)),
            ),
            observed_at=datetime(2026, 8, 11, 0, 1, tzinfo=UTC),
        )


def test_compiler_deduplicates_equal_minutes_and_rejects_correction() -> None:
    original = minute(time(9, 0), close="100")
    corrected = minute(time(9, 0), close="99")

    bars = compile_completed_kis_krx_minute_ohlcv_bars(
        market=KisDomesticMarket.KRX,
        pages=(
            KisDomesticMinuteChartPage(records=(original,)),
            KisDomesticMinuteChartPage(records=(original, minute(time(8, 59)))),
        ),
        observed_at=datetime(2026, 8, 11, 0, 1, tzinfo=UTC),
    )

    assert [(bar.timestamp, bar.close) for bar in bars] == [
        (datetime(2026, 8, 11, 0, 0, tzinfo=UTC), Decimal("100")),
        (datetime(2026, 8, 11, 0, 1, tzinfo=UTC), Decimal("100")),
    ]
    with pytest.raises(ValueError, match="KIS minute candle correction conflict"):
        compile_completed_kis_krx_minute_ohlcv_bars(
            market=KisDomesticMarket.KRX,
            pages=(
                KisDomesticMinuteChartPage(records=(original,)),
                KisDomesticMinuteChartPage(records=(corrected,)),
            ),
            observed_at=datetime(2026, 8, 11, 0, 1, tzinfo=UTC),
        )


def test_compiler_excludes_a_single_current_minute() -> None:
    assert (
        compile_completed_kis_krx_minute_ohlcv_bars(
            market=KisDomesticMarket.KRX,
            pages=(KisDomesticMinuteChartPage(records=(minute(time(9, 1)),)),),
            observed_at=datetime(2026, 8, 11, 0, 1, tzinfo=UTC),
        )
        == ()
    )


def test_kis_minute_record_rejects_seconds() -> None:
    with pytest.raises(ValueError, match="minute aligned"):
        minute(time(9, 5, 30))


@pytest.mark.parametrize(
    "market, pages, observed_at",
    [
        (
            KisDomesticMarket.NXT,
            (KisDomesticMinuteChartPage(records=(minute(time(9, 0)),)),),
            datetime(2026, 8, 11, 0, 1, tzinfo=UTC),
        ),
        (
            KisDomesticMarket.KRX,
            [KisDomesticMinuteChartPage(records=(minute(time(9, 0)),))],
            datetime(2026, 8, 11, 0, 1, tzinfo=UTC),
        ),
        (
            KisDomesticMarket.KRX,
            (object(),),
            datetime(2026, 8, 11, 0, 1, tzinfo=UTC),
        ),
        (
            KisDomesticMarket.KRX,
            (KisDomesticMinuteChartPage(records=(minute(time(9, 0)),)),),
            datetime(2026, 8, 11, 0, 1),
        ),
        (
            KisDomesticMarket.KRX,
            (KisDomesticMinuteChartPage(records=(minute(time(9, 0)),)),),
            datetime.fromisoformat("2026-08-11T09:01:00+09:00"),
        ),
    ],
)
def test_compiler_rejects_invalid_market_pages_or_observation(
    market: object, pages: object, observed_at: datetime
) -> None:
    with pytest.raises(ValueError):
        compile_completed_kis_krx_minute_ohlcv_bars(
            market=market, pages=pages, observed_at=observed_at
        )


def test_compiler_source_imports_only_kis_data_types_and_bar() -> None:
    source_path = Path(
        "src/autotrader/integrations/market_data/kis_completed_minute_bars.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.Import)
        and any(name.name.startswith("autotrader") for name in node.names)
        for node in ast.walk(tree)
    )
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("autotrader")
    ]

    assert [(node.module, [name.name for name in node.names]) for node in imports] == [
        ("autotrader.domain.completed_ohlcv", ["CompletedOhlcvBar"]),
        (
            "autotrader.integrations.brokers.kis.domestic_stock_contracts",
            [
                "KisDomesticMarket",
                "KisDomesticMinuteChartPage",
                "KisDomesticMinuteRecord",
            ],
        ),
    ]


def test_compiler_fresh_import_avoids_adapter_and_operational_modules() -> None:
    result = subprocess.run(
        (
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import sys",
                    "import autotrader.integrations.market_data."
                    "kis_completed_minute_bars",
                    "blocked = (",
                    "    'autotrader.integrations.brokers.kis.adapter',",
                    "    'autotrader.execution',",
                    "    'autotrader.runtime',",
                    "    'autotrader.transport',",
                    "    'autotrader.persistence',",
                    ")",
                    "print([name for name in sys.modules if name.startswith(blocked)])",
                )
            ),
        ),
        capture_output=True,
        check=True,
        cwd=Path.cwd(),
        text=True,
    )

    assert result.stdout == "[]\n"
