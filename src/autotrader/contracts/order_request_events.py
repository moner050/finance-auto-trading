from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


class OrderRequestOrigin(StrEnum):
    PROTECTION = "PROTECTION"
    OPERATOR = "OPERATOR"
    RECONCILIATION = "RECONCILIATION"


class ExecutionOrderRequestPayload(BaseModel):
    """Typed non-strategy order request; broker-open adoption has no variant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: OrderRequestOrigin
    protection_position_id: UUID | None
    operator_audit_id: UUID | None
    reconciliation_diff_id: UUID | None

    @model_validator(mode="after")
    def validate_exactly_one_source_evidence(self) -> ExecutionOrderRequestPayload:
        evidence = {
            OrderRequestOrigin.PROTECTION: self.protection_position_id,
            OrderRequestOrigin.OPERATOR: self.operator_audit_id,
            OrderRequestOrigin.RECONCILIATION: self.reconciliation_diff_id,
        }
        if evidence[self.origin] is None:
            raise ValueError("order request origin requires its source evidence")
        if sum(value is not None for value in evidence.values()) != 1:
            raise ValueError(
                "order request must carry exactly one source evidence field"
            )
        return self
