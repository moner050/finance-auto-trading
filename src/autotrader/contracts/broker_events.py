from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from autotrader.shared.time import require_utc


class BrokerOrderStatusPayload(BaseModel):
    """Broker-native status evidence; canonical handling never guesses identities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    broker_id: UUID
    account_id: UUID
    source_partition: str = Field(min_length=1, max_length=128)
    dedupe_key: str = Field(min_length=1, max_length=256)
    broker_order_id: str = Field(min_length=1, max_length=128)
    broker_client_order_id: str = Field(min_length=1, max_length=128)
    raw_status: str = Field(min_length=1, max_length=128)
    requested_quantity: Decimal = Field(gt=0)
    cumulative_filled_quantity: Decimal = Field(ge=0)
    source_sequence: int | None = Field(default=None, ge=1)

    @field_validator(
        "source_partition",
        "dedupe_key",
        "broker_order_id",
        "broker_client_order_id",
    )
    @classmethod
    def require_ascii(cls, value: str) -> str:
        if not value.isascii():
            raise ValueError("broker identity fields must be ASCII")
        return value


class BrokerExecutionCheckpointPayload(BaseModel):
    """Closed broker-history query evidence, never a claim inferred from a timer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    broker_id: UUID
    account_id: UUID
    source_partition: str = Field(min_length=1, max_length=128)
    broker_order_ids: tuple[str, ...] = ()
    broker_client_order_ids: tuple[str, ...] = ()
    covered_from_at: datetime
    covered_through_at: datetime
    pagination_complete: bool
    has_gap: bool
    expires_at: datetime
    query_fingerprint_hex: str = Field(min_length=64, max_length=64)

    @field_validator("covered_from_at", "covered_through_at", "expires_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return require_utc(value)

    @field_validator("query_fingerprint_hex")
    @classmethod
    def require_sha256_hex(cls, value: str) -> str:
        try:
            decoded = bytes.fromhex(value)
        except ValueError as error:
            raise ValueError("query fingerprint must be hexadecimal") from error
        if len(decoded) != 32:
            raise ValueError("query fingerprint must be SHA-256")
        return value

    @field_validator("source_partition", "broker_order_ids", "broker_client_order_ids")
    @classmethod
    def require_ascii_values(
        cls, value: str | tuple[str, ...]
    ) -> str | tuple[str, ...]:
        values = (value,) if isinstance(value, str) else value
        if any(not item or not item.isascii() for item in values):
            raise ValueError("checkpoint scope identities must be non-empty ASCII")
        return value

    @model_validator(mode="after")
    def validate_closed_interval(self) -> BrokerExecutionCheckpointPayload:
        if self.covered_from_at > self.covered_through_at:
            raise ValueError("checkpoint interval must be closed")
        if self.expires_at <= self.covered_through_at:
            raise ValueError("checkpoint expiry must be after coverage")
        if self.pagination_complete and not (
            self.broker_order_ids or self.broker_client_order_ids
        ):
            raise ValueError("complete checkpoint requires exact scope")
        return self


class BrokerExecutionPayload(BaseModel):
    """One immutable broker execution; charges are added only as explicit components."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    broker_id: UUID
    account_id: UUID
    broker_execution_id: str = Field(min_length=1, max_length=128)
    broker_order_id: str = Field(min_length=1, max_length=128)
    broker_client_order_id: str = Field(min_length=1, max_length=128)
    source_partition: str = Field(min_length=1, max_length=128)
    source_sequence: int | None = Field(default=None, ge=1)
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    executed_at: datetime

    @field_validator(
        "broker_execution_id",
        "broker_order_id",
        "broker_client_order_id",
        "source_partition",
        "currency",
    )
    @classmethod
    def require_execution_ascii(cls, value: str) -> str:
        if not value.isascii():
            raise ValueError("broker execution identity fields must be ASCII")
        return value

    @field_validator("executed_at")
    @classmethod
    def require_execution_utc(cls, value: datetime) -> datetime:
        return require_utc(value)
