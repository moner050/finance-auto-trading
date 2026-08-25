from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.strategies.david_v6.bars import exact_completed_series

START = datetime(2026, 8, 24, tzinfo=UTC)
ONE_MINUTE = timedelta(minutes=1)


def _bar(index: int) -> CompletedOhlcvBar:
    return CompletedOhlcvBar(
        timestamp=START + index * ONE_MINUTE,
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=Decimal("1"),
    )


@given(count=st.integers(min_value=1, max_value=100))
def test_forming_and_future_bars_cannot_change_a_completed_series(count: int) -> None:
    completed = tuple(_bar(index) for index in range(count))
    decision_at = START + count * ONE_MINUTE
    expected = exact_completed_series(
        completed,
        timeframe=ONE_MINUTE,
        decision_at=decision_at,
        required=count,
    )

    result = exact_completed_series(
        (*completed, _bar(count), _bar(count + 1)),
        timeframe=ONE_MINUTE,
        decision_at=decision_at,
        required=count,
    )

    assert result == expected == completed


@given(
    count=st.integers(min_value=3, max_value=100),
    data=st.data(),
)
def test_any_missing_required_timestamp_returns_no_series(
    count: int, data: st.DataObject
) -> None:
    missing = data.draw(st.integers(min_value=1, max_value=count - 2))
    bars = tuple(_bar(index) for index in range(count) if index != missing)

    result = exact_completed_series(
        bars,
        timeframe=ONE_MINUTE,
        decision_at=START + count * ONE_MINUTE,
        required=count,
    )

    assert result == ()
