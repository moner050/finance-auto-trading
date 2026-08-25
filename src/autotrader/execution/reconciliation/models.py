from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from autotrader.domain.enums import OrderStyle, Side
from autotrader.shared.decimal import require_decimal


class ReconciliationDiffKind(StrEnum):
    SNAPSHOT_INCOMPLETE = "SNAPSHOT_INCOMPLETE"
    SNAPSHOT_STALE = "SNAPSHOT_STALE"
    INTERNAL_OPEN_BROKER_MISSING = "INTERNAL_OPEN_BROKER_MISSING"
    BROKER_OPEN_INTERNAL_MISSING = "BROKER_OPEN_INTERNAL_MISSING"


@dataclass(frozen=True, slots=True)
class InternalOpenOrder:
    order_id: UUID
    broker_order_id: str
    broker_client_order_id: str


@dataclass(frozen=True, slots=True)
class BrokerOpenOrder:
    broker_order_id: str
    broker_client_order_id: str
    canonical_terms_hash: bytes

    def __post_init__(self) -> None:
        if len(self.canonical_terms_hash) != 32:
            raise ValueError("canonical_terms_hash must be SHA-256 bytes")


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    broker_id: UUID
    account_id: UUID
    complete: bool
    expires_at: datetime
    open_orders: tuple[BrokerOpenOrder, ...]

    def __post_init__(self) -> None:
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ReconciliationDiff:
    kind: ReconciliationDiffKind
    blocking: bool
    internal_order_id: UUID | None
    broker_order_id: str | None


@dataclass(frozen=True, slots=True)
class ReconciliationRun:
    id: UUID
    broker_id: UUID
    account_id: UUID
    snapshot_hash: bytes
    complete: bool
    succeeded: bool
    diffs: tuple[ReconciliationDiff, ...]
    started_at: datetime
    completed_at: datetime

    def __post_init__(self) -> None:
        if len(self.snapshot_hash) != 32:
            raise ValueError("snapshot_hash must be SHA-256 bytes")
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("reconciliation timestamps must be timezone-aware")
        if self.completed_at < self.started_at:
            raise ValueError("reconciliation cannot complete before it starts")


@dataclass(frozen=True, slots=True)
class BrokerOpenOrderAdoption:
    reconciliation_diff_id: UUID
    account_id: UUID
    broker_id: UUID
    broker_order_id: str
    broker_client_order_id: str
    instrument_id: UUID
    side: Side
    order_style: OrderStyle
    requested_quantity: Decimal
    limit_price: Decimal | None
    currency: str
    reserved_risk_amount: Decimal
    policy_version_id: UUID
    risk_snapshot_id: UUID
    observed_at: datetime
    reservation_expires_at: datetime
    payload_hash: bytes

    def __post_init__(self) -> None:
        if not self.broker_order_id or not self.broker_client_order_id:
            raise ValueError("broker order identifiers are required")
        if len(self.payload_hash) != 32:
            raise ValueError("payload_hash must be SHA-256 bytes")
        if (
            self.observed_at.tzinfo is None
            or self.reservation_expires_at.tzinfo is None
            or self.reservation_expires_at <= self.observed_at
        ):
            raise ValueError("adoption timestamps must be ordered and timezone-aware")
        if len(self.currency) != 3 or not self.currency.isupper():
            raise ValueError("currency must be a three-letter uppercase code")
        quantity = require_decimal(self.requested_quantity)
        reserved = require_decimal(self.reserved_risk_amount)
        if quantity <= 0 or reserved <= 0:
            raise ValueError("observed quantity and reserved risk must be positive")
        if self.order_style is OrderStyle.LIMIT:
            if self.limit_price is None or require_decimal(self.limit_price) <= 0:
                raise ValueError("limit adoption requires a positive limit price")
        elif self.limit_price is not None:
            raise ValueError("market adoption cannot include a limit price")
        object.__setattr__(self, "requested_quantity", quantity)
        object.__setattr__(self, "reserved_risk_amount", reserved)


@dataclass(frozen=True, slots=True)
class BrokerOpenOrderAdoptionResult:
    order_id: UUID
    broker_order_id: str
    reservation_id: UUID
    created: bool
