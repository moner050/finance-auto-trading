from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from autotrader.strategies.david_v6.order_flow import (
    OrderFlowThresholds,
    TradePrint,
    aggregate_order_flow,
)

START = datetime(2026, 8, 24, tzinfo=UTC)
THRESHOLDS = OrderFlowThresholds(
    tick_size=Decimal("1"),
    delta_p90_notional=Decimal("100"),
    atr_30s=Decimal("10"),
    ceros_near_zero_notional=Decimal("10"),
    ceros_large_notional=Decimal("40"),
)
TRADES = (
    TradePrint("a", START, Decimal("100"), Decimal("1"), False),
    TradePrint(
        "b", START + timedelta(milliseconds=100), Decimal("101"), Decimal("1"), False
    ),
    TradePrint(
        "c", START + timedelta(milliseconds=200), Decimal("102"), Decimal("1"), False
    ),
)


@given(order=st.permutations((*TRADES, TRADES[0], TRADES[1])))
def test_order_flow_is_stable_after_deterministic_provider_id_deduplication(
    order: list[TradePrint],
) -> None:
    expected = aggregate_order_flow(
        TRADES,
        window_start=START,
        window_end=START + timedelta(seconds=30),
        thresholds=THRESHOLDS,
    )

    actual = aggregate_order_flow(
        order,
        window_start=START,
        window_end=START + timedelta(seconds=30),
        thresholds=THRESHOLDS,
    )

    assert actual == expected
