from __future__ import annotations

from decimal import Decimal

from autotrader.domain.enums import Side
from autotrader.shared.decimal import require_decimal
from autotrader.shared.errors import FloatRejectedError


class RiskSizingCalculator:
    def calculate(
        self,
        *,
        risk_budget: object,
        entry_price: object,
        invalidation_price: object,
        side: Side,
    ) -> Decimal:
        try:
            budget = require_decimal(risk_budget)
            entry = require_decimal(entry_price)
            invalidation = require_decimal(invalidation_price)
        except FloatRejectedError as error:
            raise ValueError(str(error)) from error
        stop_distance = (
            entry - invalidation if side is Side.BUY else invalidation - entry
        )
        if budget <= 0 or stop_distance <= 0:
            raise ValueError("positive risk budget and stop distance are required")
        quantity = budget / stop_distance
        if quantity <= 0:
            raise ValueError("positive quantity is required")
        return quantity

    def calculate_market(
        self,
        *,
        risk_budget: object,
        invalidation_price: object,
        side: Side,
        best_bid: object,
        best_ask: object,
        policy_slippage: object,
        fresh: bool,
    ) -> Decimal:
        try:
            bid = require_decimal(best_bid)
            ask = require_decimal(best_ask)
            slippage = require_decimal(policy_slippage)
        except FloatRejectedError as error:
            raise ValueError(str(error)) from error
        if not fresh or bid <= 0 or ask <= 0 or slippage < 0:
            raise ValueError(
                "fresh positive quote and non-negative slippage are required"
            )
        worst_case_entry = ask + slippage if side is Side.BUY else bid - slippage
        if worst_case_entry <= 0:
            raise ValueError("worst-case entry price must be positive")
        return self.calculate(
            risk_budget=risk_budget,
            entry_price=worst_case_entry,
            invalidation_price=invalidation_price,
            side=side,
        )
