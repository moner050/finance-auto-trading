from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from autotrader.domain.enums import IntentType, OrderStyle, Side
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
