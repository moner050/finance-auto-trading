from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from autotrader.execution.controls.models import GateDecision


@dataclass(frozen=True, slots=True)
class DispatchAuthorizationState:
    now: datetime
    not_after: datetime
    attempt_recorded: bool
    command_owner: UUID | None
    command_fencing_token: int
    lease_owner: UUID | None
    lease_fencing_token: int | None
    lease_expires_at: datetime | None
    control_owner: UUID | None
    control_fencing_token: int | None
    control_expires_at: datetime | None
    control_armed: bool
    kill_switch_active: bool
    blocking_incident_count: int
    unresolved_unknown_count: int
    strict_reduction_proven: bool
    cancel_authorized: bool


def decide_dispatch(state: DispatchAuthorizationState) -> GateDecision:
    """Fail closed from one locked MySQL state snapshot before broker I/O."""

    if state.attempt_recorded:
        return GateDecision(False, ("DISPATCH_ALREADY_ATTEMPTED",))
    if state.now >= state.not_after:
        return GateDecision(False, ("COMMAND_EXPIRED",))
    if (
        state.lease_owner is None
        or state.lease_expires_at is None
        or state.lease_fencing_token is None
    ):
        return GateDecision(False, ("EXECUTION_LEASE_MISSING",))
    if state.lease_expires_at <= state.now:
        return GateDecision(False, ("EXECUTION_LEASE_EXPIRED",))
    if state.lease_owner != state.command_owner:
        return GateDecision(False, ("LEASE_OWNER_MISMATCH",))
    if state.lease_fencing_token != state.command_fencing_token:
        return GateDecision(False, ("LEASE_FENCING_MISMATCH",))
    if state.cancel_authorized:
        return GateDecision(True, ())
    if state.strict_reduction_proven:
        return GateDecision(True, ())
    if (
        state.control_owner is None
        or state.control_expires_at is None
        or state.control_fencing_token is None
    ):
        return GateDecision(False, ("ARM_CONTROL_MISSING",))
    if state.control_expires_at <= state.now:
        return GateDecision(False, ("ARM_CONTROL_EXPIRED",))
    if state.control_owner != state.command_owner:
        return GateDecision(False, ("ARM_CONTROL_OWNER_MISMATCH",))
    if state.control_fencing_token != state.command_fencing_token:
        return GateDecision(False, ("ARM_CONTROL_FENCING_MISMATCH",))
    if not state.control_armed:
        return GateDecision(False, ("GLOBAL_DISARMED",))
    if state.kill_switch_active:
        return GateDecision(False, ("KILL_SWITCH_ACTIVE",))
    if state.blocking_incident_count != 0:
        return GateDecision(False, ("BLOCKING_INCIDENT_ACTIVE",))
    if state.unresolved_unknown_count != 0:
        return GateDecision(False, ("UNKNOWN_ORDER_ACTIVE",))
    return GateDecision(True, ())
