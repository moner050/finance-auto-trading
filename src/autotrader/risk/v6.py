from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from uuid import UUID

from autotrader.domain.enums import OrderStyle, Side
from autotrader.shared.decimal import require_decimal
from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.models import (
    MatchedIndicator,
    SetupGrade,
    StrategyFamily,
    V6Market,
)

_DAILY_LOSS_FRACTION = Decimal("0.0075")
_WEEKLY_LOSS_FRACTION = Decimal("0.0200")
_OPEN_RISK_FRACTION = Decimal("0.0075")


@dataclass(frozen=True, slots=True)
class V6RiskRequest:
    market: V6Market
    grade: SetupGrade
    side: Side
    entry_price: Decimal
    structural_reference: Decimal
    tick_size: Decimal
    spread: Decimal
    atr_30s: Decimal | None
    atr_5m: Decimal
    session_start_equity: Decimal
    current_equity: Decimal
    daily_net_pnl: Decimal
    weekly_net_pnl: Decimal
    consecutive_net_losses: int
    current_open_structural_risk: Decimal
    quantity_step: Decimal
    cost_per_unit: Decimal
    leverage: int | None

    def __post_init__(self) -> None:
        for name, expected in (
            ("market", V6Market),
            ("grade", SetupGrade),
            ("side", Side),
        ):
            if type(getattr(self, name)) is not expected:
                raise TypeError(f"{name} must be an exact {expected.__name__}")
        for name in (
            "entry_price",
            "structural_reference",
            "tick_size",
            "atr_5m",
            "session_start_equity",
            "current_equity",
            "quantity_step",
        ):
            value = require_decimal(getattr(self, name))
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        for name in (
            "spread",
            "current_open_structural_risk",
            "cost_per_unit",
        ):
            value = require_decimal(getattr(self, name))
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
            object.__setattr__(self, name, value)
        for name in ("daily_net_pnl", "weekly_net_pnl"):
            object.__setattr__(self, name, require_decimal(getattr(self, name)))
        if self.atr_30s is not None:
            atr = require_decimal(self.atr_30s)
            if atr <= 0:
                raise ValueError("atr_30s must be positive when present")
            object.__setattr__(self, "atr_30s", atr)
        if (
            type(self.consecutive_net_losses) is not int
            or self.consecutive_net_losses < 0
        ):
            raise ValueError("consecutive_net_losses must be non-negative")
        if self.leverage is not None and (
            type(self.leverage) is not int or self.leverage <= 0
        ):
            raise ValueError("leverage must be a positive integer when present")


@dataclass(frozen=True, slots=True)
class V6RiskAuthority:
    allowed: bool
    blocker_codes: tuple[str, ...]
    risk_base: Decimal
    risk_fraction: Decimal
    risk_budget: Decimal
    structural_reference: Decimal
    stop_price: Decimal
    quantity: Decimal
    stop_distance_atr5m: Decimal


@dataclass(frozen=True, slots=True)
class V6RiskContext:
    decision_id: UUID
    setup_id: UUID
    feature_snapshot_id: UUID
    family: StrategyFamily
    order_style: OrderStyle
    matched_indicators: tuple[MatchedIndicator, ...]
    mandatory_indicator_codes: frozenset[str]
    risk_request: V6RiskRequest
    target_price: Decimal
    valid_until: datetime

    def __post_init__(self) -> None:
        for name in ("decision_id", "setup_id", "feature_snapshot_id"):
            value = getattr(self, name)
            if not isinstance(value, UUID) or value.version != 7:
                raise ValueError(f"{name} must be UUIDv7")
        if type(self.family) is not StrategyFamily:
            raise TypeError("family must be an exact StrategyFamily")
        if type(self.order_style) is not OrderStyle:
            raise TypeError("order_style must be an exact OrderStyle")
        if type(self.matched_indicators) is not tuple or any(
            type(indicator) is not MatchedIndicator
            for indicator in self.matched_indicators
        ):
            raise TypeError("matched_indicators must contain exact values")
        if type(self.mandatory_indicator_codes) is not frozenset or any(
            type(code) is not str or not code or code.strip() != code
            for code in self.mandatory_indicator_codes
        ):
            raise ValueError("mandatory indicator codes must be trimmed text")
        if type(self.risk_request) is not V6RiskRequest:
            raise TypeError("risk_request must be an exact V6RiskRequest")
        target = require_decimal(self.target_price)
        if target <= 0:
            raise ValueError("target_price must be positive")
        object.__setattr__(self, "target_price", target)
        object.__setattr__(self, "valid_until", require_utc(self.valid_until))


def evaluate_v6_risk(request: V6RiskRequest) -> V6RiskAuthority:
    if type(request) is not V6RiskRequest:
        raise TypeError("request must be an exact V6RiskRequest")
    request.__post_init__()
    blockers: list[str] = []
    risk_base = min(request.session_start_equity, request.current_equity)
    risk_fraction = _risk_fraction(request.market, request.grade, blockers)
    risk_budget = risk_base * risk_fraction

    buffer_values = (
        Decimal(4) * request.tick_size,
        Decimal(2) * request.spread,
    )
    if request.market is V6Market.BINANCE_USDM:
        if request.atr_30s is None:
            blockers.append("BINANCE_ATR30S_REQUIRED")
            buffer = max(buffer_values)
        else:
            buffer = max(*buffer_values, Decimal("0.10") * request.atr_30s)
        if request.leverage is None:
            blockers.append("BINANCE_LEVERAGE_REQUIRED")
        elif request.leverage > 7:
            blockers.append("BINANCE_LEVERAGE_LIMIT")
    else:
        buffer = max(buffer_values)
        if request.atr_30s is not None:
            blockers.append("CASH_ATR30S_NOT_APPLICABLE")
        if request.leverage is not None:
            blockers.append("CASH_LEVERAGE_NOT_APPLICABLE")

    if request.side is Side.BUY:
        if request.structural_reference >= request.entry_price:
            blockers.append("INVALID_LONG_STRUCTURAL_REFERENCE")
        stop_price = request.structural_reference - buffer
    else:
        if request.structural_reference <= request.entry_price:
            blockers.append("INVALID_SHORT_STRUCTURAL_REFERENCE")
        stop_price = request.structural_reference + buffer
    if stop_price <= 0:
        blockers.append("NON_POSITIVE_STOP")

    stop_distance = abs(request.entry_price - stop_price)
    distance_atr = stop_distance / request.atr_5m
    if distance_atr < Decimal("0.40"):
        blockers.append("STOP_DISTANCE_BELOW_0_40_ATR5M")
    if distance_atr > Decimal("1.50"):
        blockers.append("STOP_DISTANCE_ABOVE_1_50_ATR5M")

    if request.daily_net_pnl <= -(risk_base * _DAILY_LOSS_FRACTION):
        blockers.append("DAILY_LOSS_LIMIT")
    if request.weekly_net_pnl <= -(risk_base * _WEEKLY_LOSS_FRACTION):
        blockers.append("WEEKLY_LOSS_LIMIT")
    if request.consecutive_net_losses >= 2:
        blockers.append("CONSECUTIVE_LOSS_LIMIT")
    if (
        request.current_open_structural_risk + risk_budget
        > risk_base * _OPEN_RISK_FRACTION
    ):
        blockers.append("OPEN_RISK_LIMIT")

    per_unit_loss = stop_distance + request.cost_per_unit
    raw_quantity = risk_budget / per_unit_loss if per_unit_loss > 0 else Decimal(0)
    quantity = (raw_quantity / request.quantity_step).to_integral_value(
        rounding=ROUND_FLOOR
    ) * request.quantity_step
    if quantity <= 0:
        blockers.append("ROUNDED_QUANTITY_ZERO")
    canonical_blockers = tuple(sorted(set(blockers)))
    return V6RiskAuthority(
        allowed=not canonical_blockers,
        blocker_codes=canonical_blockers,
        risk_base=risk_base,
        risk_fraction=risk_fraction,
        risk_budget=risk_budget,
        structural_reference=request.structural_reference,
        stop_price=stop_price,
        quantity=(quantity if not canonical_blockers else Decimal(0)),
        stop_distance_atr5m=distance_atr,
    )


def _risk_fraction(
    market: V6Market,
    grade: SetupGrade,
    blockers: list[str],
) -> Decimal:
    if grade is SetupGrade.REJECT:
        blockers.append("SETUP_REJECTED")
        return Decimal(0)
    if market in {V6Market.KRX_CASH, V6Market.US_CASH}:
        if grade is SetupGrade.A_CANDIDATE:
            blockers.append("CASH_A_CANDIDATE_UNSUPPORTED")
            return Decimal(0)
        return Decimal("0.0025") if grade is SetupGrade.A else Decimal("0.0015")
    return Decimal("0.0050") if grade is SetupGrade.A else Decimal("0.0025")


__all__ = (
    "V6RiskAuthority",
    "V6RiskContext",
    "V6RiskRequest",
    "evaluate_v6_risk",
)
