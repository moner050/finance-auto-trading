from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.research.david_v6.backtest import exact_next_bar_fill

SIGNAL_AT = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)
TIMEFRAME = timedelta(minutes=5)


def _bar(timestamp: datetime, close: Decimal) -> CompletedOhlcvBar:
    opening = Decimal("100")
    return CompletedOhlcvBar(
        timestamp=timestamp,
        open=opening,
        high=max(opening, close),
        low=min(opening, close),
        close=close,
        volume=Decimal("1"),
    )


@given(
    future_closes=st.lists(
        st.decimals(
            min_value=Decimal("90"),
            max_value=Decimal("110"),
            allow_nan=False,
            allow_infinity=False,
            places=2,
        ),
        max_size=20,
    )
)
def test_appending_future_bars_cannot_change_the_prior_exact_fill(
    future_closes: list[Decimal],
) -> None:
    immediate = _bar(SIGNAL_AT + TIMEFRAME, Decimal("101"))
    expected = exact_next_bar_fill(
        signal_at=SIGNAL_AT,
        timeframe=TIMEFRAME,
        bars=(immediate,),
    )
    future = tuple(
        _bar(SIGNAL_AT + (index + 2) * TIMEFRAME, close)
        for index, close in enumerate(future_closes)
    )

    actual = exact_next_bar_fill(
        signal_at=SIGNAL_AT,
        timeframe=TIMEFRAME,
        bars=(*future, immediate),
    )

    assert actual == expected
