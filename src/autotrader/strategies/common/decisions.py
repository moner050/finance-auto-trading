from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from autotrader.domain.enums import IntentType, OrderStyle, Side
from autotrader.shared.decimal import decimal_to_string, require_decimal
from autotrader.shared.time import require_utc


class StrategyStatus(StrEnum):
    SHADOW = "SHADOW"
    LIVE_APPROVED = "LIVE_APPROVED"
    RETIRED = "RETIRED"


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    id: UUID
    strategy_version_id: UUID
    setup_id: UUID
    feature_snapshot_id: UUID
    instrument_id: UUID
    intent_type: IntentType
    side: Side
    order_style: OrderStyle
    planned_entry: Decimal
    trigger_price: Decimal
    invalidation_price: Decimal
    generated_at: datetime
    valid_until: datetime
    session_type: str
    source_v6_decision_id: UUID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "planned_entry", require_decimal(self.planned_entry))
        object.__setattr__(self, "trigger_price", require_decimal(self.trigger_price))
        object.__setattr__(
            self, "invalidation_price", require_decimal(self.invalidation_price)
        )
        if any(
            price <= 0
            for price in (
                self.planned_entry,
                self.trigger_price,
                self.invalidation_price,
            )
        ):
            raise ValueError("strategy prices must be positive")
        object.__setattr__(self, "generated_at", require_utc(self.generated_at))
        object.__setattr__(self, "valid_until", require_utc(self.valid_until))
        if self.valid_until <= self.generated_at:
            raise ValueError("valid_until must be after generated_at")
        if self.source_v6_decision_id is not None:
            if self.source_v6_decision_id.version != 7:
                raise ValueError("source_v6_decision_id must be UUIDv7")
            if self.source_v6_decision_id != self.id:
                raise ValueError("v6 decision and generic signal identities must match")

    def decision_hash(self) -> bytes:
        payload = asdict(self)
        if payload["source_v6_decision_id"] is None:
            del payload["source_v6_decision_id"]
        serialized = {
            key: decimal_to_string(value)
            if isinstance(value, Decimal)
            else value.isoformat()
            if isinstance(value, datetime)
            else str(value)
            if isinstance(value, (UUID, StrEnum))
            else value
            for key, value in payload.items()
        }
        return hashlib.sha256(
            json.dumps(
                serialized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).digest()


def validate_strategy_promotion(
    *,
    status: StrategyStatus,
    research_only: bool,
    enabled_hard_rule_count: int,
    verified_source_link_count: int,
) -> None:
    if research_only and status is StrategyStatus.LIVE_APPROVED:
        raise ValueError("research-only strategy cannot be live approved")
    if enabled_hard_rule_count < 0 or verified_source_link_count < 0:
        raise ValueError("source counts must be non-negative")
    if (
        status is StrategyStatus.LIVE_APPROVED
        and verified_source_link_count < enabled_hard_rule_count
    ):
        raise ValueError("every enabled hard rule requires a verified source")
