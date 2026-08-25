from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.strategies.david_v6.metodo import evaluate_metodo
from autotrader.strategies.david_v6.models import V6Market

START = datetime(2025, 1, 1, tzinfo=UTC)


def _bar(index: int, close: Decimal) -> CompletedOhlcvBar:
    return CompletedOhlcvBar(
        timestamp=START + timedelta(days=index),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=Decimal("1"),
    )


@given(
    future_closes=st.lists(
        st.decimals(
            min_value=Decimal("1"),
            max_value=Decimal("1000"),
            places=4,
            allow_nan=False,
            allow_infinity=False,
        ),
        min_size=0,
        max_size=20,
    )
)
def test_future_daily_bars_cannot_change_an_earlier_metodo_fact(
    future_closes: list[Decimal],
) -> None:
    closes = (Decimal("100"),) * 200 + (Decimal("99"), Decimal("110"))
    completed = tuple(_bar(index, close) for index, close in enumerate(closes))
    decision_at = completed[-1].timestamp + timedelta(days=1)
    expected = evaluate_metodo(
        market=V6Market.US_CASH,
        daily_bars=completed,
        decision_at=decision_at,
    )
    future = tuple(
        _bar(len(completed) + index, close) for index, close in enumerate(future_closes)
    )

    actual = evaluate_metodo(
        market=V6Market.US_CASH,
        daily_bars=(*completed, *future),
        decision_at=decision_at,
    )

    assert actual == expected
