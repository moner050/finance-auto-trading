from __future__ import annotations

from decimal import Decimal

import pytest

from autotrader.domain.enums import Side
from autotrader.risk.sizing import RiskSizingCalculator


def test_sizing_rejects_float_and_invalid_stop() -> None:
    calculator = RiskSizingCalculator()
    with pytest.raises(ValueError):
        calculator.calculate(
            risk_budget=Decimal("100"),
            entry_price=10.0,
            invalidation_price=Decimal("9"),
            side=Side.BUY,
        )
    with pytest.raises(ValueError, match="stop"):
        calculator.calculate(
            risk_budget=Decimal("100"),
            entry_price=Decimal("10"),
            invalidation_price=Decimal("10"),
            side=Side.BUY,
        )


def test_sizing_returns_positive_decimal_quantity() -> None:
    assert RiskSizingCalculator().calculate(
        risk_budget=Decimal("100"),
        entry_price=Decimal("10"),
        invalidation_price=Decimal("8"),
        side=Side.BUY,
    ) == Decimal("50")


def test_sizing_rejects_non_protective_stop_and_uses_market_worst_case() -> None:
    calculator = RiskSizingCalculator()
    with pytest.raises(ValueError, match="stop"):
        calculator.calculate(
            risk_budget=Decimal("100"),
            entry_price=Decimal("10"),
            invalidation_price=Decimal("11"),
            side=Side.BUY,
        )

    assert calculator.calculate_market(
        risk_budget=Decimal("100"),
        invalidation_price=Decimal("8"),
        side=Side.BUY,
        best_bid=Decimal("9"),
        best_ask=Decimal("10"),
        policy_slippage=Decimal("1"),
        fresh=True,
    ) == Decimal("33.33333333333333333333333333")
