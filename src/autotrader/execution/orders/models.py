from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from autotrader.domain.enums import OrderStyle, Side
from autotrader.shared.decimal import require_decimal


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class CommandType(StrEnum):
    SUBMIT = "SUBMIT"
    CANCEL = "CANCEL"
    REPLACE = "REPLACE"


@dataclass(frozen=True, slots=True)
class BrokerOrderLinkState:
    id: UUID
    broker_order_id: str
    link_sequence: int
    exposure_bearing: bool
    status: OrderStatus

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }


@dataclass(frozen=True, slots=True)
class BrokerStatusWatermark:
    source_partition: str
    last_contiguous_sequence: int


@dataclass(frozen=True, slots=True)
class Order:
    id: UUID
    order_intent_id: UUID
    risk_decision_id: UUID
    account_id: UUID
    instrument_id: UUID
    side: Side
    order_style: OrderStyle
    requested_quantity: Decimal
    limit_price: Decimal | None
    status: OrderStatus
    aggregate_version: int
    broker_client_order_id: str
    created_at: datetime
    trigger_price: Decimal | None = None

    def __post_init__(self) -> None:
        quantity = require_decimal(self.requested_quantity)
        if quantity <= 0:
            raise ValueError("order requested quantity must be positive")
        object.__setattr__(self, "requested_quantity", quantity)
        if self.order_style is OrderStyle.LIMIT:
            if self.limit_price is None or require_decimal(self.limit_price) <= 0:
                raise ValueError("limit order requires a positive limit price")
        elif self.limit_price is not None:
            raise ValueError("market order cannot carry a limit price")
        if self.trigger_price is not None:
            if self.order_style is not OrderStyle.MARKET:
                raise ValueError("a triggered order must be a market order")
            if require_decimal(self.trigger_price) <= 0:
                raise ValueError("trigger_price must be positive")
        if self.aggregate_version < 0:
            raise ValueError("aggregate version must be non-negative")


@dataclass(frozen=True, slots=True)
class BrokerOrderStatusEvent:
    broker_id: UUID
    account_id: UUID
    source_partition: str
    dedupe_key: str
    raw_status: str
    occurred_at: datetime
    source_sequence: int | None = None
    broker_order_id: str | None = None
    order_id: UUID | None = None
    broker_client_order_id: str | None = None
    requested_quantity: Decimal | None = None
    cumulative_filled_quantity: Decimal | None = None
    observed_at: datetime | None = None
    payload_hash: bytes = b""


@dataclass(frozen=True, slots=True)
class OrderDomainEvent:
    order_id: UUID
    aggregate_version: int
    status: OrderStatus
    raw_status: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class OrderTransition:
    order: Order
    event: OrderDomainEvent
    watermark: BrokerStatusWatermark | None = None

    @classmethod
    def create(
        cls,
        *,
        order: Order,
        status: OrderStatus,
        raw_status: str,
        occurred_at: datetime,
    ) -> OrderTransition:
        updated = replace(
            order, status=status, aggregate_version=order.aggregate_version + 1
        )
        return cls(
            order=updated,
            event=OrderDomainEvent(
                order_id=order.id,
                aggregate_version=updated.aggregate_version,
                status=status,
                raw_status=raw_status,
                occurred_at=occurred_at,
            ),
        )


@dataclass(frozen=True, slots=True)
class DeferredBrokerStatus:
    order: Order
    event: BrokerOrderStatusEvent
    reason: str
    missing_from_sequence: int | None = None


def all_exposure_links_terminal(links: tuple[BrokerOrderLinkState, ...]) -> bool:
    return all(link.is_terminal for link in links if link.exposure_bearing)


@dataclass(frozen=True, slots=True)
class BrokerOrderCommand:
    id: UUID
    order_id: UUID
    account_id: UUID
    instrument_id: UUID
    command_type: CommandType
    target_aggregate_version: int
    idempotency_key: str
    command_sequence: int
    canonical_payload_hash: bytes
    broker_client_order_id: str
    target_broker_order_id: str | None
    replaces_command_id: UUID | None
    origin_type: str
    authority_class: str
    owner_runtime_instance_id: UUID | None
    fencing_token: int
    not_after: datetime
    side: Side
    order_style: OrderStyle
    quantity: Decimal
    limit_price: Decimal | None
    time_in_force: str
    trigger_price: Decimal | None = None
    status: str = "PENDING"
    dispatch_attempted_at: datetime | None = None
