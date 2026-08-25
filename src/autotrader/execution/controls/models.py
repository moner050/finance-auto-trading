from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from autotrader.config.settings import RuntimeMode


class KillSwitchLevel(StrEnum):
    NONE = "NONE"
    BLOCK_NEW_EXPOSURE = "BLOCK_NEW_EXPOSURE"
    EMERGENCY = "EMERGENCY"


class ExposureEffect(StrEnum):
    INCREASE = "INCREASE"
    REDUCE = "REDUCE"
    FLAT = "FLAT"
    FLIP = "FLIP"
    READ_ONLY = "READ_ONLY"
    UNKNOWN = "UNKNOWN"


class GateAction(StrEnum):
    SUBMIT = "SUBMIT"
    CANCEL = "CANCEL"
    REPLACE = "REPLACE"


@dataclass(frozen=True, slots=True)
class ArmLease:
    owner_runtime_instance_id: UUID
    acquired_at: datetime
    expires_at: datetime
    fencing_token: int
    row_version: int


@dataclass(frozen=True, slots=True)
class TradingControl:
    scope_type: str
    scope_key: str
    kill_switch_level: KillSwitchLevel
    fencing_token: int
    row_version: int


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SubmissionContext:
    now: datetime
    action: GateAction
    runtime_mode: RuntimeMode
    allow_live: bool
    account_environment: str
    local_runtime_instance_id: UUID
    locally_armed: bool
    arm_lease: ArmLease | None
    database_writable: bool
    market_data_fresh: bool
    active_kill_switch: bool
    blocking_incident_count: int
    unresolved_unknown_count: int
    blocking_reconciliation_count: int
    exposure_effect: ExposureEffect
