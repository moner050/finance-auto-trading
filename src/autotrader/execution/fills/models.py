from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from autotrader.domain.enums import Side
from autotrader.shared.decimal import require_decimal


class ChargeEffect(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class ChargeLegRole(StrEnum):
    ENTRY = "ENTRY"
    EXIT_TARGET = "EXIT_TARGET"
    EXIT_STOP = "EXIT_STOP"
    EXIT_OTHER = "EXIT_OTHER"


class ChargeBasis(StrEnum):
    PER_UNIT = "PER_UNIT"
    PER_NOTIONAL = "PER_NOTIONAL"
    PER_ORDER_MINIMUM = "PER_ORDER_MINIMUM"


@dataclass(frozen=True, slots=True)
class ExecutionChargeComponent:
    component_ordinal: int
    amount: Decimal
    currency: str
    charge_kind: str
    effect: ChargeEffect
    leg_role: ChargeLegRole
    charge_basis: ChargeBasis
    basis_quantity: Decimal | None
    basis_notional: Decimal | None

    def __post_init__(self) -> None:
        if self.component_ordinal < 0:
            raise ValueError("charge component ordinal must be non-negative")
        if require_decimal(self.amount) <= 0:
            raise ValueError("charge amount must be positive")
        if len(self.currency) != 3 or not self.currency.isalpha():
            raise ValueError("charge currency must be an ISO code")
        if not self.charge_kind:
            raise ValueError("charge kind is required")
        quantity = self.basis_quantity
        notional = self.basis_notional
        if self.charge_basis is ChargeBasis.PER_UNIT:
            if (
                quantity is None
                or require_decimal(quantity) <= 0
                or notional is not None
            ):
                raise ValueError("PER_UNIT requires only a positive basis quantity")
        elif self.charge_basis is ChargeBasis.PER_NOTIONAL:
            if (
                notional is None
                or require_decimal(notional) <= 0
                or quantity is not None
            ):
                raise ValueError("PER_NOTIONAL requires only a positive basis notional")
        elif quantity is not None or notional is not None:
            raise ValueError("PER_ORDER_MINIMUM requires no basis")


@dataclass(frozen=True, slots=True)
class BrokerExecutionEvent:
    id: UUID
    broker_id: UUID
    account_id: UUID
    order_id: UUID
    broker_order_id: str
    broker_client_order_id: str
    broker_execution_id: str
    source_partition: str
    source_sequence: int | None
    instrument_id: UUID
    side: Side
    quantity: Decimal
    price: Decimal
    charges: tuple[ExecutionChargeComponent, ...]
    currency: str
    executed_at: datetime
    observed_at: datetime
    payload_hash: bytes

    def __post_init__(self) -> None:
        if require_decimal(self.quantity) <= 0 or require_decimal(self.price) <= 0:
            raise ValueError("execution quantity and price must be positive")
        if tuple(component.component_ordinal for component in self.charges) != tuple(
            range(len(self.charges))
        ):
            raise ValueError("charge component ordinals must be contiguous")


@dataclass(frozen=True, slots=True)
class Fill:
    id: UUID
    order_id: UUID
    broker_execution_id: str
    quantity: Decimal
    price: Decimal
    side: Side
    executed_at: datetime
    charges: tuple[ExecutionChargeComponent, ...]


@dataclass(frozen=True, slots=True)
class BrokerExecutionWatermark:
    broker_id: UUID
    account_id: UUID
    source_partition: str
    contiguous_through_sequence: int | None
    has_gap: bool
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ReservationConsumption:
    initial: Decimal
    consumed: Decimal
    remaining: Decimal
    released: Decimal

    def apply_attributable_fill(self, risk_amount: Decimal) -> ReservationConsumption:
        amount = require_decimal(risk_amount)
        if amount < 0:
            raise ValueError("attributable fill risk cannot be negative")
        reclaimed = min(self.released, amount)
        consumed_from_remaining = min(self.remaining, amount - reclaimed)
        consumed = self.consumed + reclaimed + consumed_from_remaining
        remaining = self.remaining - consumed_from_remaining
        released = self.released - reclaimed
        return ReservationConsumption(self.initial, consumed, remaining, released)
