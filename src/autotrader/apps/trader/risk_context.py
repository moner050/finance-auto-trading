"""Pricing the account for one evaluation.

Held apart from the paper composition because the Shadow loop needs it too,
and importing that module to reach it would pull a paper broker into a loop
whose whole point is having nothing to submit through.

The budget is the operator's money. Nothing here defaults it: every field
decides the size of an order that a later mode will actually place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import OrderStyle, Side
from autotrader.risk.models import V6RiskPolicySnapshot
from autotrader.risk.v6 import V6RiskContext, V6RiskRequest
from autotrader.shared.ids import new_uuid7
from autotrader.strategies.david_v6.models import SetupGrade, StrategyFamily, V6Market

ATR_WINDOW = 14


@dataclass(frozen=True, slots=True)
class AccountBudget:
    """The operator's money, which no strategy may invent."""

    session_start_equity: Decimal
    current_equity: Decimal
    quantity_step: Decimal
    tick_size: Decimal
    spread: Decimal
    cost_per_unit: Decimal
    leverage: int
    valid_for: timedelta = timedelta(minutes=5)


class BinanceRiskContexts:
    """Price the account for one evaluation, from the bars it just saw."""

    def __init__(
        self,
        *,
        budget: AccountBudget,
        policy: V6RiskPolicySnapshot,
        side: Side = Side.BUY,
    ) -> None:
        self._budget = budget
        self._policy = policy
        self._side = side

    def build(
        self, *, bars: tuple[CompletedOhlcvBar, ...], now: datetime
    ) -> V6RiskContext | None:
        atr = average_true_range(bars)
        if atr is None:
            return None
        budget = self._budget
        entry = bars[-1].close
        return V6RiskContext(
            decision_id=new_uuid7(),
            setup_id=new_uuid7(),
            feature_snapshot_id=new_uuid7(),
            family=StrategyFamily.HLIT,
            order_style=OrderStyle.LIMIT,
            # The tick replaces both from the assembled evidence.
            matched_indicators=(),
            mandatory_indicator_codes=frozenset(),
            risk_request=V6RiskRequest(
                market=V6Market.BINANCE_USDM,
                grade=SetupGrade.NORMAL,
                side=self._side,
                entry_price=entry,
                # A placeholder the exhaustion overrides once it is confirmed.
                structural_reference=(
                    entry - atr if self._side is Side.BUY else entry + atr
                ),
                tick_size=budget.tick_size,
                spread=budget.spread,
                atr_30s=atr,
                atr_5m=atr,
                session_start_equity=budget.session_start_equity,
                current_equity=budget.current_equity,
                daily_net_pnl=Decimal(0),
                weekly_net_pnl=Decimal(0),
                consecutive_net_losses=0,
                current_open_structural_risk=Decimal(0),
                quantity_step=budget.quantity_step,
                cost_per_unit=budget.cost_per_unit,
                leverage=budget.leverage,
            ),
            policy=self._policy,
            target_price=entry,
            valid_until=now + budget.valid_for,
        )


def average_true_range(
    bars: tuple[CompletedOhlcvBar, ...], window: int = ATR_WINDOW
) -> Decimal | None:
    if len(bars) <= window:
        return None
    ranges: list[Decimal] = []
    for previous, current in zip(bars[-window - 1 : -1], bars[-window:], strict=True):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    average = sum(ranges, start=Decimal(0)) / Decimal(len(ranges))
    return average if average > 0 else None


__all__ = (
    "ATR_WINDOW",
    "AccountBudget",
    "BinanceRiskContexts",
    "average_true_range",
)
