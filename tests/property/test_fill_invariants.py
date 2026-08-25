from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from autotrader.execution.fills.models import ReservationConsumption


@given(
    initial=st.integers(min_value=0, max_value=10_000),
    fill=st.integers(min_value=0, max_value=20_000),
)
def test_reservation_consumption_preserves_non_negative_accounting(
    initial: int, fill: int
) -> None:
    before = ReservationConsumption(
        initial=Decimal(initial),
        consumed=Decimal("0"),
        remaining=Decimal(initial),
        released=Decimal("0"),
    )
    after = before.apply_attributable_fill(Decimal(fill))

    assert after.consumed + after.remaining + after.released == after.initial
    assert min(after.consumed, after.remaining, after.released) >= 0
