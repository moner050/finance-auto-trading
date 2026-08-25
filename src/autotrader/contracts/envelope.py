from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from autotrader.shared.ids import uuid_to_binary
from autotrader.shared.serialization import canonical_json_bytes, sha256_bytes
from autotrader.shared.time import require_utc

PayloadT = TypeVar("PayloadT", bound=BaseModel)


class EventEnvelope(BaseModel, Generic[PayloadT]):  # noqa: UP046
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    event_type: str = Field(min_length=1)
    schema_version: int = Field(ge=1)
    occurred_at: datetime
    observed_at: datetime
    producer: str = Field(min_length=1)
    partition_key: str = Field(min_length=1)
    aggregate_type: str = Field(min_length=1)
    aggregate_id: UUID
    aggregate_version: int = Field(ge=1)
    correlation_id: UUID
    causation_id: UUID | None
    trace_id: str = Field(min_length=1)
    payload: PayloadT

    @field_validator("occurred_at", "observed_at")
    @classmethod
    def validate_utc(cls, value: datetime) -> datetime:
        return require_utc(value)

    @field_validator("event_id", "aggregate_id", "correlation_id", "causation_id")
    @classmethod
    def validate_uuid7(cls, value: UUID | None) -> UUID | None:
        if value is not None:
            uuid_to_binary(value)
        return value

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    def sha256(self) -> bytes:
        return sha256_bytes(self.canonical_bytes())
