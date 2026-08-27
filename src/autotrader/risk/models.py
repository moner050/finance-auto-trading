from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from autotrader.domain.enums import Side
from autotrader.shared.decimal import require_decimal
from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.models import SetupGrade, V6Market

# Section 11.4 approves one percent of the account as the most a single trade
# may put at risk. A policy may sit below it — the approved cash policies sit
# at 0.25% and Binance at 0.75% — but nothing may sit above it. Without this
# the only bound is (0, 1], which permits risking the whole account on one
# trade and calling it a policy.
TRADE_RISK_CEILING = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class LockedAccountSnapshot:
    account_id: UUID
    environment: str
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    currency: str
    captured_at: datetime
    row_version: int


@dataclass(frozen=True, slots=True)
class LockedPosition:
    instrument_id: UUID
    quantity: Decimal
    available_quantity: Decimal
    average_cost: Decimal
    position_risk_amount: Decimal
    currency: str
    row_version: int


@dataclass(frozen=True, slots=True)
class OpenOrderExposure:
    order_id: UUID
    instrument_id: UUID
    side: Side
    unfilled_quantity: Decimal
    worst_case_price: Decimal
    risk_amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class RiskReservationView:
    reservation_id: UUID
    initial_risk_amount: Decimal
    consumed_risk_amount: Decimal
    remaining_risk_amount: Decimal
    released_risk_amount: Decimal
    status: str


@dataclass(frozen=True, slots=True)
class RiskBudgetAnchorView:
    scope_type: str
    scope_key: str
    currency: str
    position_risk_amount: Decimal
    remaining_reservation_amount: Decimal
    hard_limit_amount: Decimal
    row_version: int


@dataclass(frozen=True, slots=True)
class RiskSnapshotView:
    id: UUID
    account_id: UUID
    as_of: datetime
    currency: str | None
    equity: Decimal
    cash: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    open_risk: Decimal
    daily_realized_pnl: Decimal
    daily_unrealized_pnl: Decimal
    drawdown: Decimal
    position_hash: bytes
    open_order_hash: bytes
    settlement_asset: str | None = None

    def __post_init__(self) -> None:
        _require_asset_denomination(
            currency=self.currency,
            settlement_asset=self.settlement_asset,
        )


@dataclass(frozen=True, slots=True)
class V6RiskPolicySnapshot:
    policy_version_id: UUID
    market: V6Market
    normal_risk_fraction: Decimal
    a_candidate_risk_fraction: Decimal | None
    a_risk_fraction: Decimal
    absolute_trade_risk_fraction: Decimal
    daily_loss_fraction: Decimal
    weekly_loss_fraction: Decimal
    max_consecutive_losses: int
    max_open_structural_risk_fraction: Decimal
    account_age: timedelta
    risk_age: timedelta
    quote_age: timedelta
    provider_age: timedelta
    stream_gap_age: timedelta | None
    completed_intraday_bar_arrival_age: timedelta
    daily_requires_authoritative_close: bool

    def __post_init__(self) -> None:
        _require_uuid7(self.policy_version_id, "policy_version_id")
        if type(self.market) is not V6Market:
            raise TypeError("market must be an exact V6Market")
        for name in (
            "normal_risk_fraction",
            "a_risk_fraction",
            "absolute_trade_risk_fraction",
            "daily_loss_fraction",
            "weekly_loss_fraction",
            "max_open_structural_risk_fraction",
        ):
            value = require_decimal(getattr(self, name))
            if not Decimal("0") < value <= Decimal("1"):
                raise ValueError(f"{name} must be in (0, 1]")
            object.__setattr__(self, name, value)
        if self.a_candidate_risk_fraction is not None:
            candidate = require_decimal(self.a_candidate_risk_fraction)
            if not Decimal("0") < candidate <= Decimal("1"):
                raise ValueError("a_candidate_risk_fraction must be in (0, 1]")
            object.__setattr__(self, "a_candidate_risk_fraction", candidate)
        if (
            type(self.max_consecutive_losses) is not int
            or self.max_consecutive_losses <= 0
        ):
            raise ValueError("max_consecutive_losses must be positive")
        if type(self.daily_requires_authoritative_close) is not bool:
            raise TypeError("daily_requires_authoritative_close must be bool")
        if any(
            value > self.absolute_trade_risk_fraction
            for value in (
                self.normal_risk_fraction,
                self.a_risk_fraction,
                *(
                    ()
                    if self.a_candidate_risk_fraction is None
                    else (self.a_candidate_risk_fraction,)
                ),
            )
        ):
            raise ValueError("grade risk cannot exceed the absolute trade ceiling")
        if self.absolute_trade_risk_fraction > TRADE_RISK_CEILING:
            # Every grade sits at or below this fraction, so bounding it bounds
            # them all.
            raise ValueError("absolute trade risk cannot exceed one percent")
        if self.market is V6Market.BINANCE_USDM:
            if self.a_candidate_risk_fraction is None or self.stream_gap_age is None:
                raise ValueError("Binance policy requires candidate and stream ages")
            if self.daily_requires_authoritative_close:
                raise ValueError("Binance daily evidence uses the UTC session")
        else:
            if (
                self.a_candidate_risk_fraction is not None
                or self.stream_gap_age is not None
            ):
                raise ValueError("cash policy cannot define futures-only fields")
            if not self.daily_requires_authoritative_close:
                raise ValueError("cash policy requires an authoritative close")
        for name in (
            "account_age",
            "risk_age",
            "quote_age",
            "provider_age",
            "completed_intraday_bar_arrival_age",
        ):
            _require_positive_timedelta(getattr(self, name), name)
        if self.stream_gap_age is not None:
            _require_positive_timedelta(self.stream_gap_age, "stream_gap_age")

    def risk_fraction_for(self, grade: SetupGrade) -> Decimal | None:
        if grade is SetupGrade.REJECT:
            return Decimal("0")
        if grade is SetupGrade.NORMAL:
            return self.normal_risk_fraction
        if grade is SetupGrade.A_CANDIDATE:
            return self.a_candidate_risk_fraction
        if grade is SetupGrade.A:
            return self.a_risk_fraction
        raise TypeError("grade must be an exact SetupGrade")

    def is_account_fresh(self, *, age: timedelta) -> bool:
        return _is_fresh(age=age, maximum=self.account_age)

    def is_risk_fresh(self, *, age: timedelta) -> bool:
        return _is_fresh(age=age, maximum=self.risk_age)

    def is_quote_fresh(self, *, age: timedelta) -> bool:
        return _is_fresh(age=age, maximum=self.quote_age)

    def is_provider_fresh(self, *, age: timedelta) -> bool:
        return _is_fresh(age=age, maximum=self.provider_age)

    def is_stream_gap_fresh(self, *, age: timedelta) -> bool:
        return self.stream_gap_age is not None and _is_fresh(
            age=age,
            maximum=self.stream_gap_age,
        )

    def is_completed_intraday_bar_fresh(self, *, age: timedelta) -> bool:
        return _is_fresh(
            age=age,
            maximum=self.completed_intraday_bar_arrival_age,
        )


@dataclass(frozen=True, slots=True)
class V6SessionRiskAnchor:
    id: UUID
    account_id: UUID
    policy_version_id: UUID
    account_snapshot_id: UUID
    risk_snapshot_id: UUID
    market: V6Market
    session_key: str
    session_started_at: datetime
    captured_at: datetime
    starting_equity: Decimal
    currency: str | None
    settlement_asset: str | None
    evidence_hash: bytes

    def __post_init__(self) -> None:
        for name in (
            "id",
            "account_id",
            "policy_version_id",
            "account_snapshot_id",
            "risk_snapshot_id",
        ):
            _require_uuid7(getattr(self, name), name)
        if type(self.market) is not V6Market:
            raise TypeError("market must be an exact V6Market")
        if not self.session_key or self.session_key.strip() != self.session_key:
            raise ValueError("session_key must be non-empty and trimmed")
        object.__setattr__(
            self,
            "session_started_at",
            require_utc(self.session_started_at),
        )
        object.__setattr__(self, "captured_at", require_utc(self.captured_at))
        if self.captured_at < self.session_started_at:
            raise ValueError("anchor capture cannot precede session start")
        starting_equity = require_decimal(self.starting_equity)
        if starting_equity <= 0:
            raise ValueError("starting_equity must be positive")
        object.__setattr__(self, "starting_equity", starting_equity)
        _require_asset_denomination(
            currency=self.currency,
            settlement_asset=self.settlement_asset,
        )
        if type(self.evidence_hash) is not bytes or len(self.evidence_hash) != 32:
            raise ValueError("evidence_hash must be SHA-256")

    def risk_base(self, *, current_equity: Decimal) -> Decimal:
        current = require_decimal(current_equity)
        if current <= 0:
            raise ValueError("current_equity must be positive")
        return min(self.starting_equity, current)


@dataclass(frozen=True, slots=True)
class RiskQuote:
    instrument_id: UUID
    bid: Decimal
    ask: Decimal
    currency: str
    as_of: datetime


@dataclass(frozen=True, slots=True)
class RiskPolicySnapshot:
    policy_version_id: UUID
    active: bool
    max_total_risk: Decimal
    max_position_value: Decimal
    max_daily_loss: Decimal
    max_drawdown: Decimal
    max_slippage_bps: Decimal
    max_account_snapshot_age: timedelta
    max_risk_snapshot_age: timedelta
    max_market_data_age: timedelta


@dataclass(frozen=True, slots=True)
class TradingControlSnapshot:
    trading_enabled: bool


@dataclass(frozen=True, slots=True)
class RiskContext:
    decision_at: datetime
    account_snapshot: LockedAccountSnapshot
    risk_snapshot: RiskSnapshotView
    positions: tuple[LockedPosition, ...]
    open_orders: tuple[OpenOrderExposure, ...]
    active_reservations: tuple[RiskReservationView, ...]
    budget_anchors: tuple[RiskBudgetAnchorView, ...]
    quote: RiskQuote
    active_policy: RiskPolicySnapshot
    trading_control: TradingControlSnapshot
    blocking_incident_count: int
    unresolved_unknown_count: int
    blocking_reconciliation_count: int
    position_hash: bytes
    open_order_hash: bytes


class RiskOutcome(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REDUCE = "REDUCE"
    OBSERVED_BLOCKING = "OBSERVED_BLOCKING"


@dataclass(frozen=True, slots=True)
class RiskDecision:
    id: UUID
    order_intent_id: UUID
    risk_snapshot_id: UUID
    outcome: RiskOutcome
    requested_quantity: Decimal
    reason_codes: tuple[str, ...]
    approved_quantity: Decimal
    approved_limit_price: Decimal | None
    reserved_risk_amount: Decimal
    currency: str
    policy_version_id: UUID
    decided_at: datetime
    decision_hash: bytes


def _require_asset_denomination(
    *, currency: str | None, settlement_asset: str | None
) -> None:
    if (currency is None) == (settlement_asset is None):
        raise ValueError("exactly one of currency and settlement_asset is required")
    if currency is not None and (
        len(currency) != 3 or not currency.isascii() or not currency.isupper()
    ):
        raise ValueError("currency must be a three-letter uppercase code")
    if settlement_asset is not None and (
        not 1 <= len(settlement_asset) <= 16
        or not settlement_asset.isascii()
        or not settlement_asset.isupper()
    ):
        raise ValueError("settlement_asset must be an uppercase asset code")


def _require_positive_timedelta(value: timedelta, name: str) -> None:
    if type(value) is not timedelta or value <= timedelta(0):
        raise ValueError(f"{name} must be positive")


def _is_fresh(*, age: timedelta, maximum: timedelta) -> bool:
    return type(age) is timedelta and timedelta(0) <= age <= maximum


def _require_uuid7(value: object, name: str) -> None:
    if type(value) is not UUID or value.version != 7:
        raise ValueError(f"{name} must be UUIDv7")
