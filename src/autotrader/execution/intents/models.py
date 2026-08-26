from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from autotrader.domain.enums import IntentType, OrderStyle, Side
from autotrader.shared.decimal import require_decimal
from autotrader.shared.ids import new_uuid7


@dataclass(frozen=True, slots=True)
class AccountCandidate:
    id: UUID
    broker_code: str
    market_code: str
    environment: str
    enabled: bool
    policy_key: str
    policy_active: bool


class IntentOrigin(StrEnum):
    STRATEGY = "STRATEGY"
    PROTECTION = "PROTECTION"
    OPERATOR = "OPERATOR"
    RECONCILIATION = "RECONCILIATION"


@dataclass(frozen=True, slots=True)
class SizingApproved:
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class StrategyIntentRequest:
    """Explicit strategy evidence whose order terms have already been approved."""

    source_id: UUID
    instrument_id: UUID
    intent_type: IntentType
    side: Side
    order_style: OrderStyle
    terms: OrderTerms


@dataclass(frozen=True, slots=True)
class MarketQuote:
    bid: Decimal
    ask: Decimal
    fresh: bool


@dataclass(frozen=True, slots=True)
class OrderTerms:
    requested_quantity: Decimal
    limit_price: Decimal | None
    # A protective stop rests until the market reaches this price.
    trigger_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OrderIntent:
    origin: IntentOrigin
    source_id: UUID
    account_id: UUID
    instrument_id: UUID
    intent_type: IntentType
    side: Side
    order_style: OrderStyle
    quantity: Decimal
    limit_price: Decimal | None
    idempotency_key: str
    id: UUID = field(default_factory=new_uuid7)
    trigger_price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.trigger_price is None:
            return
        # A stop-limit fails to fill in its own way, and the paper broker
        # refuses one for that reason. Accepting it here would let an order
        # exist that no broker in this system can carry.
        if self.order_style is not OrderStyle.MARKET:
            raise ValueError("a triggered intent must be a market order")
        if require_decimal(self.trigger_price) <= 0:
            raise ValueError("trigger_price must be positive")


@dataclass(frozen=True, slots=True)
class OperatorRequest:
    audit_id: UUID
    instrument_id: UUID
    intent_type: IntentType
    side: Side
    order_style: OrderStyle
    terms: OrderTerms
    quote: MarketQuote | None = None


@dataclass(frozen=True, slots=True)
class ProtectionRequest:
    locked_position_id: UUID
    reason_code: str
    instrument_id: UUID
    intent_type: IntentType
    side: Side
    order_style: OrderStyle
    terms: OrderTerms
    quote: MarketQuote | None = None

    def __post_init__(self) -> None:
        if not self.reason_code.strip():
            raise ValueError("protection reason_code is required")


@dataclass(frozen=True, slots=True)
class ReconciliationRequest:
    blocking_diff_id: UUID
    instrument_id: UUID
    intent_type: IntentType
    side: Side
    order_style: OrderStyle
    terms: OrderTerms
    quote: MarketQuote | None = None
