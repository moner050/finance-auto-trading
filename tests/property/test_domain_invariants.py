from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from autotrader.shared.decimal import require_non_negative


@given(
    st.decimals(
        min_value=Decimal("0"),
        max_value=Decimal("1000000000"),
        allow_nan=False,
        allow_infinity=False,
        places=4,
    )
)
def test_non_negative_decimal_invariant(value: Decimal) -> None:
    assert require_non_negative(value) == value


@given(
    st.decimals(
        max_value=Decimal("-0.0001"),
        allow_nan=False,
        allow_infinity=False,
        places=4,
    )
)
def test_negative_decimal_is_rejected(value: Decimal) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        require_non_negative(value)
