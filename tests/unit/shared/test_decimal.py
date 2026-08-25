from decimal import Decimal

import pytest

from autotrader.shared.decimal import (
    decimal_to_string,
    require_decimal,
    require_non_negative,
)


def test_decimal_round_trip_is_a_string_at_the_contract_boundary() -> None:
    value = Decimal("123.4500")

    assert decimal_to_string(value) == "123.4500"
    assert require_decimal(value) is value


def test_decimal_helpers_reject_float_and_negative_values() -> None:
    with pytest.raises(TypeError, match="float"):
        require_decimal(1.0)
    with pytest.raises(ValueError, match="non-negative"):
        require_non_negative(Decimal("-0.01"))
