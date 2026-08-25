from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest

from autotrader.config.settings import RuntimeMode
from autotrader.execution.controls.gates import SubmissionGate
from autotrader.execution.controls.models import (
    ArmLease,
    ExposureEffect,
    GateAction,
    SubmissionContext,
)

NOW = datetime(2026, 8, 9, tzinfo=UTC)
RUNTIME_INSTANCE_ID = uuid7()


def approved_increase_context() -> SubmissionContext:
    return SubmissionContext(
        now=NOW,
        action=GateAction.SUBMIT,
        runtime_mode=RuntimeMode.PAPER,
        allow_live=False,
        account_environment="PAPER",
        local_runtime_instance_id=RUNTIME_INSTANCE_ID,
        locally_armed=True,
        arm_lease=ArmLease(
            owner_runtime_instance_id=RUNTIME_INSTANCE_ID,
            acquired_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=1),
            fencing_token=1,
            row_version=1,
        ),
        database_writable=True,
        market_data_fresh=True,
        active_kill_switch=False,
        blocking_incident_count=0,
        unresolved_unknown_count=0,
        blocking_reconciliation_count=0,
        exposure_effect=ExposureEffect.INCREASE,
    )


def test_submission_gate_allows_only_a_fully_authorized_paper_increase() -> None:
    decision = SubmissionGate().evaluate(approved_increase_context())

    assert decision.allowed is True
    assert decision.reason_codes == ()


@pytest.mark.parametrize(
    ("change", "reason_code"),
    [
        ({"runtime_mode": RuntimeMode.SHADOW}, "RUNTIME_MODE_DENIED"),
        ({"account_environment": "LIVE"}, "ACCOUNT_ENVIRONMENT_MISMATCH"),
        ({"locally_armed": False}, "LOCAL_DISARMED"),
        ({"arm_lease": None}, "ARM_LEASE_MISSING"),
        ({"database_writable": False}, "DATABASE_UNWRITABLE"),
        ({"market_data_fresh": False}, "MARKET_DATA_STALE"),
        ({"active_kill_switch": True}, "KILL_SWITCH_ACTIVE"),
        ({"blocking_incident_count": 1}, "BLOCKING_INCIDENT_ACTIVE"),
        ({"unresolved_unknown_count": 1}, "UNKNOWN_ORDER_ACTIVE"),
        ({"blocking_reconciliation_count": 1}, "RECONCILIATION_BLOCKING"),
        ({"exposure_effect": ExposureEffect.UNKNOWN}, "EXPOSURE_EFFECT_UNKNOWN"),
    ],
)
def test_submission_gate_denies_each_failed_increase_gate(
    change: dict[str, object], reason_code: str
) -> None:
    decision = SubmissionGate().evaluate(replace(approved_increase_context(), **change))

    assert decision.allowed is False
    assert reason_code in decision.reason_codes


def test_submission_gate_rejects_expired_or_foreign_arm_lease() -> None:
    approved = approved_increase_context()
    assert approved.arm_lease is not None
    lease = approved.arm_lease

    expired = SubmissionGate().evaluate(
        replace(approved, arm_lease=replace(lease, expires_at=NOW))
    )
    foreign = SubmissionGate().evaluate(
        replace(
            approved,
            arm_lease=replace(lease, owner_runtime_instance_id=uuid7()),
        )
    )

    assert expired.allowed is False
    assert "ARM_LEASE_EXPIRED" in expired.reason_codes
    assert foreign.allowed is False
    assert "ARM_LEASE_NOT_OWNED" in foreign.reason_codes


def test_submission_gate_rejects_invalid_negative_blocker_counts() -> None:
    decision = SubmissionGate().evaluate(
        replace(approved_increase_context(), blocking_incident_count=-1)
    )

    assert decision.allowed is False
    assert "BLOCKING_INCIDENT_STATE_INVALID" in decision.reason_codes


def test_verified_reduction_cancel_bypasses_disarm_and_kill_switch_only() -> None:
    context = replace(
        approved_increase_context(),
        action=GateAction.CANCEL,
        runtime_mode=RuntimeMode.SHADOW,
        locally_armed=False,
        active_kill_switch=True,
        market_data_fresh=False,
        exposure_effect=ExposureEffect.REDUCE,
    )

    decision = SubmissionGate().evaluate(context)

    assert decision.allowed is True


def test_reduction_cancel_still_requires_owner_lease_and_database_write() -> None:
    context = replace(
        approved_increase_context(),
        action=GateAction.CANCEL,
        runtime_mode=RuntimeMode.SHADOW,
        locally_armed=False,
        active_kill_switch=True,
        exposure_effect=ExposureEffect.REDUCE,
        database_writable=False,
    )

    decision = SubmissionGate().evaluate(context)

    assert decision.allowed is False
    assert "DATABASE_UNWRITABLE" in decision.reason_codes
