from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class OrderCreatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    order_intent_id: str = Field(min_length=1)
    risk_decision_id: str = Field(min_length=1)
    status: str = Field(pattern="^CREATED$")
    requested_quantity: Decimal = Field(gt=0)


class FillAppliedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    broker_execution_id: str = Field(min_length=1)
    quantity: Decimal = Field(gt=0)
    total_filled_quantity: Decimal = Field(gt=0)
    overfill: bool


class OrderStatusAppliedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    broker_order_id: str = Field(min_length=1)
    raw_status: str = Field(min_length=1)
    status: str = Field(min_length=1)
    terminal_release_pending: bool


class ReservationReleasedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    released_risk_amount: Decimal = Field(ge=0)
    release_reason: str = Field(min_length=1)


class BrokerOrderAdoptedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reconciliation_diff_id: str = Field(min_length=1)
    broker_order_id: str = Field(min_length=1)
    reservation_id: str = Field(min_length=1)
