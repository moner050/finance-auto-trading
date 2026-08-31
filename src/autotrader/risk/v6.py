from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from uuid import UUID

from autotrader.domain.enums import OrderStyle, Side
from autotrader.risk.models import TRADE_RISK_CEILING, V6RiskPolicySnapshot
from autotrader.shared.decimal import require_decimal
from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.grading import HYPOTHESIS_CODES
from autotrader.strategies.david_v6.models import (
    MatchedIndicator,
    SetupGrade,
    StrategyFamily,
    V6Market,
)

_MAX_SPREAD_TICKS = Decimal(3)
# Section 6 refuses a fixed daily trade count and marks this bound as an
# anti-overtrading safety candidate rather than one of David's values.
SESSION_TRADE_UPPER_BOUND = 8
# A ceiling, not a setting. Section 21 approves seven for Binance USD-M, and a
# policy row that could raise it would turn the approved limit into a default.
MAX_LEVERAGE = 7

# What the setup grade is allowed to decide.
#
# The grade comes from section 21.3, which titles itself "연구용 점수표" and
# says outright: "이 점수는 David의 직접식이 아니다 ... Ablation으로 검증하기
# 위한 연구 프레임이다". Section 15.2 then classifies the Cyborg large-move
# determination the score stands in for as `score_only`, and the document's
# governing principle is that estimated rules "백테스트·섀도 거래를 통과하기
# 전에는 주문 권한을 주지 않는다".
#
# Sizing is order authority. A grade of A drew twice the fraction of NORMAL on
# Binance, so a research score was deciding how large a real order is. While
# this stays SCORE_ONLY the grade is computed, recorded and available to
# ablation, and it cannot enlarge a position.
#
# Raising it is a deliberate edit here, not a policy row, because the promotion
# gate proves that sessions ran rather than that this table means anything.
SCORE_ONLY = "SCORE_ONLY"
DECIDES_SIZE = "DECIDES_SIZE"
RESEARCH_SCORE_AUTHORITY = SCORE_ONLY


@dataclass(frozen=True, slots=True)
class ApprovedCapital:
    """What section 11.4 approved each market to trade with.

    The engine sizes from live equity, not from this, so nothing here bounds a
    trade. It is the figure the operator approved, kept beside the ceilings so
    the screen can show what an account is supposed to hold and the operator
    can notice when it does not.
    """

    market: V6Market
    amount: Decimal
    unit: str
    kind: str


APPROVED_CAPITAL = (
    ApprovedCapital(V6Market.KRX_CASH, Decimal("1000000"), "KRW", "계좌 자본"),
    ApprovedCapital(V6Market.US_CASH, Decimal("2000"), "USD", "계좌 자본"),
    ApprovedCapital(V6Market.BINANCE_USDM, Decimal("2000"), "USDT", "증거금"),
)


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
    session_trade_count: int = 0
    session_objective_reached: bool = False
    size_multiplier: Decimal = Decimal(1)
    max_quantity: Decimal | None = None

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
        for name in ("consecutive_net_losses", "session_trade_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.leverage is not None and (
            type(self.leverage) is not int or self.leverage <= 0
        ):
            raise ValueError("leverage must be a positive integer when present")
        if type(self.session_objective_reached) is not bool:
            raise TypeError("session_objective_reached must be bool")
        multiplier = require_decimal(self.size_multiplier)
        if not Decimal(0) < multiplier <= Decimal(1):
            raise ValueError("size_multiplier must be within zero and one")
        object.__setattr__(self, "size_multiplier", multiplier)
        if self.max_quantity is not None:
            cap = require_decimal(self.max_quantity)
            if cap <= 0:
                raise ValueError("max_quantity must be positive when present")
            object.__setattr__(self, "max_quantity", cap)


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
    # The policy in force for this evaluation, carried with the request it
    # sizes rather than looked up inside the engine, so what a decision was
    # measured against is part of what the decision recorded.
    policy: V6RiskPolicySnapshot
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
        # Requiring a hypothesis is order authority in its strongest form:
        # without it there is no entry at all. The document forbids exactly
        # that until an estimated rule has passed backtest and shadow, so the
        # rule lives here rather than in whoever assembles the set.
        hypotheses = self.mandatory_indicator_codes & HYPOTHESIS_CODES
        if hypotheses:
            raise ValueError(
                "a reverse-engineered indicator cannot be mandatory: "
                + ", ".join(sorted(hypotheses))
            )
        if type(self.risk_request) is not V6RiskRequest:
            raise TypeError("risk_request must be an exact V6RiskRequest")
        if type(self.policy) is not V6RiskPolicySnapshot:
            raise TypeError("policy must be an exact V6RiskPolicySnapshot")
        if self.policy.market is not self.risk_request.market:
            raise ValueError("the policy and the request must name the same market")
        target = require_decimal(self.target_price)
        if target <= 0:
            raise ValueError("target_price must be positive")
        object.__setattr__(self, "target_price", target)
        object.__setattr__(self, "valid_until", require_utc(self.valid_until))


def evaluate_v6_risk(
    request: V6RiskRequest, *, policy: V6RiskPolicySnapshot
) -> V6RiskAuthority:
    """Size a trade against the policy that is actually in force.

    The policy is required rather than defaulted. These fractions decide how
    much money moves, and an engine that falls back to a built-in number when
    nobody handed it one would keep trading after a policy was retired,
    against limits the operator can no longer see or change.
    """
    if type(request) is not V6RiskRequest:
        raise TypeError("request must be an exact V6RiskRequest")
    if type(policy) is not V6RiskPolicySnapshot:
        raise TypeError("policy must be an exact V6RiskPolicySnapshot")
    if policy.market is not request.market:
        # A cash policy applied to a futures request would size a leveraged
        # position with the fractions approved for an unleveraged one.
        raise ValueError("the policy and the request must name the same market")
    request.__post_init__()
    blockers: list[str] = []
    risk_base = min(request.session_start_equity, request.current_equity)
    risk_fraction = _risk_fraction(policy, request.grade, blockers)
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
        elif request.leverage > MAX_LEVERAGE:
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

    if request.spread > _MAX_SPREAD_TICKS * request.tick_size:
        blockers.append("SPREAD_ABOVE_THREE_TICKS")

    stop_distance = abs(request.entry_price - stop_price)
    distance_atr = stop_distance / request.atr_5m
    if distance_atr < Decimal("0.40"):
        blockers.append("STOP_DISTANCE_BELOW_0_40_ATR5M")
    if distance_atr > Decimal("1.50"):
        blockers.append("STOP_DISTANCE_ABOVE_1_50_ATR5M")

    if request.daily_net_pnl <= -(risk_base * policy.daily_loss_fraction):
        blockers.append("DAILY_LOSS_LIMIT")
    if request.weekly_net_pnl <= -(risk_base * policy.weekly_loss_fraction):
        blockers.append("WEEKLY_LOSS_LIMIT")
    if request.consecutive_net_losses >= policy.max_consecutive_losses:
        blockers.append("CONSECUTIVE_LOSS_LIMIT")
    if request.session_trade_count >= SESSION_TRADE_UPPER_BOUND:
        blockers.append("SESSION_TRADE_UPPER_BOUND")
    # Section 9.3: once the objective is met, close the screen and leave.
    if request.session_objective_reached:
        blockers.append("SESSION_OBJECTIVE_REACHED")
    if (
        request.current_open_structural_risk + risk_budget
        > risk_base * policy.max_open_structural_risk_fraction
    ):
        blockers.append("OPEN_RISK_LIMIT")

    per_unit_loss = stop_distance + request.cost_per_unit
    raw_quantity = risk_budget / per_unit_loss if per_unit_loss > 0 else Decimal(0)
    # Section 7.1 controls the open by size rather than by a waiting period.
    raw_quantity *= request.size_multiplier
    if request.max_quantity is not None:
        raw_quantity = min(raw_quantity, request.max_quantity)
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
    policy: V6RiskPolicySnapshot,
    grade: SetupGrade,
    blockers: list[str],
) -> Decimal:
    """The fraction this grade is allowed, from the policy in force.

    A cash policy carries no A-candidate fraction, and its absence is what
    says the grade is unsupported there. Reading that from the policy rather
    than from the market keeps one answer to the question.

    While `RESEARCH_SCORE_AUTHORITY` is SCORE_ONLY the elevated fractions are
    held down to the normal one. Only the size is capped: a grade that blocks
    still blocks, because refusing to trade and trading smaller are different
    answers and this must never turn the first into the second.
    """
    if grade is SetupGrade.REJECT:
        blockers.append("SETUP_REJECTED")
        return Decimal(0)
    if grade is SetupGrade.A_CANDIDATE:
        if policy.a_candidate_risk_fraction is None:
            blockers.append("CASH_A_CANDIDATE_UNSUPPORTED")
            return Decimal(0)
        return _allowed(policy.a_candidate_risk_fraction, policy)
    if grade is SetupGrade.A:
        return _allowed(policy.a_risk_fraction, policy)
    return policy.normal_risk_fraction


def _allowed(fraction: Decimal, policy: V6RiskPolicySnapshot) -> Decimal:
    """An elevated fraction, or the normal one while the score is research."""
    if RESEARCH_SCORE_AUTHORITY == SCORE_ONLY:
        return min(fraction, policy.normal_risk_fraction)
    return fraction


__all__ = (
    "APPROVED_CAPITAL",
    "DECIDES_SIZE",
    "MAX_LEVERAGE",
    "RESEARCH_SCORE_AUTHORITY",
    "SCORE_ONLY",
    "SESSION_TRADE_UPPER_BOUND",
    "TRADE_RISK_CEILING",
    "ApprovedCapital",
    "V6RiskAuthority",
    "V6RiskContext",
    "V6RiskRequest",
    "evaluate_v6_risk",
)
