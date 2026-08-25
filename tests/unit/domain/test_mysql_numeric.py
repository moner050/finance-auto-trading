from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, Inexact, Rounded, localcontext

import pytest

from autotrader.domain.mysql_numeric import canonical_mysql_numeric_38_18


def test_numeric_38_18_rounds_positive_half_up_independent_of_context() -> None:
    with localcontext() as context:
        context.prec = 4
        result = canonical_mysql_numeric_38_18(
            Decimal("100.6666666666666666665"), "weighted cost"
        )

    assert result == Decimal("100.666666666666666667")


def test_numeric_38_18_canonicalizes_signed_zero_and_rejects_overflow() -> None:
    zero = canonical_mysql_numeric_38_18(Decimal("-0.0000000000000000004"), "zero")
    assert zero == Decimal(0)
    assert zero.as_tuple().sign == 0
    assert zero.as_tuple().exponent == 0

    with pytest.raises(ValueError, match=r"NUMERIC\(38,18\)"):
        canonical_mysql_numeric_38_18(
            Decimal("100000000000000000000.000000000000000000"), "overflow"
        )


def test_numeric_38_18_ignores_ambient_rounding_and_traps() -> None:
    with localcontext() as context:
        context.prec = 4
        context.rounding = ROUND_DOWN
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        result = canonical_mysql_numeric_38_18(
            Decimal("100.6666666666666666665"), "weighted cost"
        )

    assert result == Decimal("100.666666666666666667")
