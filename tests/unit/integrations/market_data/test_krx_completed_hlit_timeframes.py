from __future__ import annotations

import ast
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.integrations.market_data.krx_completed_hlit_timeframes import (
    HlitTimeframe,
    compile_completed_krx_hlit_timeframe,
)


def minute_bar(timestamp: str, *, close: Decimal = Decimal("1")) -> CompletedOhlcvBar:
    return CompletedOhlcvBar(
        timestamp=datetime.fromisoformat(timestamp),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=Decimal("1"),
    )


def test_one_minute_timeframe_preserves_completed_minute_bars() -> None:
    bars = (
        minute_bar("2026-08-11T00:01:00+00:00", close=Decimal("1")),
        minute_bar("2026-08-11T00:02:00+00:00", close=Decimal("2")),
    )

    assert (
        compile_completed_krx_hlit_timeframe(
            bars=bars,
            timeframe=HlitTimeframe.ONE_MINUTE,
        )
        == bars
    )


def test_five_minute_bucket_uses_kst_end_and_aggregates() -> None:
    result = compile_completed_krx_hlit_timeframe(
        bars=tuple(
            minute_bar(f"2026-08-11T00:0{end}:00+00:00", close=Decimal(end))
            for end in range(1, 6)
        ),
        timeframe=HlitTimeframe.FIVE_MINUTES,
    )

    assert result == (
        CompletedOhlcvBar(
            timestamp=datetime(2026, 8, 11, 0, 5, tzinfo=UTC),
            open=Decimal("1"),
            high=Decimal("5"),
            low=Decimal("1"),
            close=Decimal("5"),
            volume=Decimal("5"),
        ),
    )


def test_fifteen_minute_bucket_uses_kst_end_and_aggregates() -> None:
    result = compile_completed_krx_hlit_timeframe(
        bars=tuple(
            minute_bar(f"2026-08-11T00:{end:02}:00+00:00", close=Decimal(end))
            for end in range(1, 16)
        ),
        timeframe=HlitTimeframe.FIFTEEN_MINUTES,
    )

    assert result == (
        CompletedOhlcvBar(
            timestamp=datetime(2026, 8, 11, 0, 15, tzinfo=UTC),
            open=Decimal("1"),
            high=Decimal("15"),
            low=Decimal("1"),
            close=Decimal("15"),
            volume=Decimal("15"),
        ),
    )


def test_one_hour_bucket_uses_kst_end_and_aggregates() -> None:
    result = compile_completed_krx_hlit_timeframe(
        bars=tuple(
            minute_bar(
                f"2026-08-11T{end // 60:02}:{end % 60:02}:00+00:00",
                close=Decimal(end),
            )
            for end in range(1, 61)
        ),
        timeframe=HlitTimeframe.ONE_HOUR,
    )

    assert result == (
        CompletedOhlcvBar(
            timestamp=datetime(2026, 8, 11, 1, 0, tzinfo=UTC),
            open=Decimal("1"),
            high=Decimal("60"),
            low=Decimal("1"),
            close=Decimal("60"),
            volume=Decimal("60"),
        ),
    )


def test_partial_five_minute_bucket_is_ignored() -> None:
    partial = (
        minute_bar("2026-08-11T00:01:00+00:00"),
        minute_bar("2026-08-11T00:02:00+00:00"),
        minute_bar("2026-08-11T00:03:00+00:00"),
        minute_bar("2026-08-11T00:04:00+00:00"),
    )

    assert (
        compile_completed_krx_hlit_timeframe(
            bars=partial,
            timeframe=HlitTimeframe.FIVE_MINUTES,
        )
        == ()
    )


def test_compiler_rejects_a_non_utc_source_timestamp() -> None:
    bar = minute_bar("2026-08-11T00:01:00+00:00")
    object.__setattr__(
        bar,
        "timestamp",
        datetime(2026, 8, 11, 9, 1, tzinfo=ZoneInfo("Asia/Seoul")),
    )

    with pytest.raises(
        ValueError,
        match="KRX HLIT timeframe bars require consecutive completed minutes",
    ):
        compile_completed_krx_hlit_timeframe(
            bars=(bar,),
            timeframe=HlitTimeframe.FIVE_MINUTES,
        )


@pytest.mark.parametrize(
    "bars",
    [
        (
            minute_bar("2026-08-11T00:01:00+00:00"),
            minute_bar("2026-08-11T00:01:00+00:00"),
        ),
        (
            minute_bar("2026-08-11T00:02:00+00:00"),
            minute_bar("2026-08-11T00:01:00+00:00"),
        ),
        (minute_bar("2026-08-11T00:01:00+00:00"), object()),
        (
            minute_bar("2026-08-11T00:01:00+00:00"),
            minute_bar("2026-08-11T00:03:00+00:00"),
        ),
        (minute_bar("2026-08-11T00:01:30+00:00"),),
        [minute_bar("2026-08-11T00:01:00+00:00")],
    ],
)
def test_compiler_rejects_nonconsecutive_or_nonimmutable_completed_minutes(
    bars: object,
) -> None:
    with pytest.raises(
        ValueError,
        match="KRX HLIT timeframe bars require consecutive completed minutes",
    ):
        compile_completed_krx_hlit_timeframe(
            bars=bars,
            timeframe=HlitTimeframe.FIVE_MINUTES,
        )


@pytest.mark.parametrize("timeframe", ("5m", "15m", "1h", None))
def test_compiler_rejects_non_enum_or_unsupported_interval(timeframe: object) -> None:
    with pytest.raises(
        ValueError, match="KRX HLIT timeframe requires a supported interval"
    ):
        compile_completed_krx_hlit_timeframe(
            bars=(),
            timeframe=timeframe,
        )


def test_bucket_does_not_combine_two_kst_dates() -> None:
    assert (
        compile_completed_krx_hlit_timeframe(
            bars=tuple(
                minute_bar(timestamp)
                for timestamp in (
                    "2026-08-11T14:56:00+00:00",
                    "2026-08-11T14:57:00+00:00",
                    "2026-08-11T14:58:00+00:00",
                    "2026-08-11T14:59:00+00:00",
                    "2026-08-11T15:00:00+00:00",
                )
            ),
            timeframe=HlitTimeframe.FIVE_MINUTES,
        )
        == ()
    )


def test_compiler_source_imports_only_completed_ohlcv_bar() -> None:
    source_path = Path(
        "src/autotrader/integrations/market_data/krx_completed_hlit_timeframes.py"
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
    ]


def test_compiler_fresh_import_avoids_operational_modules() -> None:
    result = subprocess.run(
        (
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import sys",
                    "import autotrader.integrations.market_data."
                    "krx_completed_hlit_timeframes",
                    "blocked = (",
                    "    'autotrader.integrations.brokers',",
                    "    'autotrader.execution',",
                    "    'autotrader.runtime',",
                    "    'autotrader.transport',",
                    "    'autotrader.persistence',",
                    "    'autotrader.application',",
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
