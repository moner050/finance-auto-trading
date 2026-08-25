from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar


def test_completed_ohlcv_bar_preserves_distinct_open_and_close() -> None:
    bar = CompletedOhlcvBar(
        timestamp=datetime(2026, 8, 11, 0, 1, tzinfo=UTC),
        open=Decimal("99"),
        high=Decimal("103"),
        low=Decimal("98"),
        close=Decimal("100"),
        volume=Decimal("1200"),
    )

    assert bar.open == Decimal("99")
    assert bar.close == Decimal("100")


@pytest.mark.parametrize(
    "field, value",
    [
        ("timestamp", datetime(2026, 8, 11, 0, 1)),
        (
            "timestamp",
            datetime(2026, 8, 11, 9, 1, tzinfo=timezone(timedelta(hours=9))),
        ),
        ("open", "99"),
        ("high", Decimal("0")),
        ("low", Decimal("0")),
        ("close", Decimal("0")),
        ("volume", Decimal("-1")),
    ],
)
def test_completed_ohlcv_bar_rejects_invalid_field_values(
    field: str, value: object
) -> None:
    values: dict[str, object] = {
        "timestamp": datetime(2026, 8, 11, 0, 1, tzinfo=UTC),
        "open": Decimal("99"),
        "high": Decimal("103"),
        "low": Decimal("98"),
        "close": Decimal("100"),
        "volume": Decimal("1200"),
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError)):
        CompletedOhlcvBar(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "open_price, high, low, close",
    [
        (Decimal("99"), Decimal("98"), Decimal("99"), Decimal("99")),
        (Decimal("97"), Decimal("103"), Decimal("98"), Decimal("100")),
        (Decimal("99"), Decimal("103"), Decimal("98"), Decimal("104")),
    ],
)
def test_completed_ohlcv_bar_rejects_invalid_price_ranges(
    open_price: Decimal, high: Decimal, low: Decimal, close: Decimal
) -> None:
    with pytest.raises(ValueError):
        CompletedOhlcvBar(
            timestamp=datetime(2026, 8, 11, 0, 1, tzinfo=UTC),
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=Decimal("1200"),
        )
