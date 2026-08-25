from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid7

from autotrader.execution.dispatch.authorization import (
    DispatchAuthorizationState,
    decide_dispatch,
)


def state(**changes: object) -> DispatchAuthorizationState:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    owner = uuid7()
    values: dict[str, object] = {
        "now": now,
        "not_after": now + timedelta(minutes=1),
        "attempt_recorded": False,
        "command_owner": owner,
        "command_fencing_token": 3,
        "lease_owner": owner,
        "lease_fencing_token": 3,
        "lease_expires_at": now + timedelta(minutes=1),
        "control_owner": owner,
        "control_fencing_token": 3,
        "control_expires_at": now + timedelta(minutes=1),
        "control_armed": True,
        "kill_switch_active": False,
        "blocking_incident_count": 0,
        "unresolved_unknown_count": 0,
        "strict_reduction_proven": False,
        "cancel_authorized": False,
    }
    values.update(changes)
    return DispatchAuthorizationState(**values)  # type: ignore[arg-type]


def test_authorization_requires_current_matching_lease_and_control() -> None:
    assert decide_dispatch(state()).allowed is True
    decision = decide_dispatch(state(lease_fencing_token=2))
    assert decision.allowed is False
    assert decision.reason_codes == ("LEASE_FENCING_MISMATCH",)


def test_authorization_fails_closed_for_expiry_or_unknown_order() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    expired = decide_dispatch(state(not_after=now))
    unknown = decide_dispatch(state(unresolved_unknown_count=1))

    assert expired.reason_codes == ("COMMAND_EXPIRED",)
    assert unknown.reason_codes == ("UNKNOWN_ORDER_ACTIVE",)


def test_proven_protective_reduction_bypasses_arm_controls_but_not_lease() -> None:
    reduction = decide_dispatch(
        state(
            strict_reduction_proven=True,
            control_owner=None,
            control_fencing_token=None,
            control_expires_at=None,
            control_armed=False,
            kill_switch_active=True,
            blocking_incident_count=1,
            unresolved_unknown_count=1,
        )
    )
    stale_lease = decide_dispatch(
        state(strict_reduction_proven=True, lease_fencing_token=2)
    )

    assert reduction.allowed is True
    assert stale_lease.reason_codes == ("LEASE_FENCING_MISMATCH",)


def test_authorized_cancel_bypasses_exposure_gates_but_not_lease() -> None:
    cancel = decide_dispatch(
        state(
            cancel_authorized=True,
            control_owner=None,
            control_fencing_token=None,
            control_expires_at=None,
            control_armed=False,
            kill_switch_active=True,
            blocking_incident_count=1,
            unresolved_unknown_count=1,
        )
    )
    stale_lease = decide_dispatch(state(cancel_authorized=True, lease_fencing_token=2))

    assert cancel.allowed is True
    assert stale_lease.reason_codes == ("LEASE_FENCING_MISMATCH",)
