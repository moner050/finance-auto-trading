from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid7

from hypothesis import given
from hypothesis import strategies as st

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


@given(
    locally_armed=st.booleans(),
    database_writable=st.booleans(),
    market_data_fresh=st.booleans(),
    active_kill_switch=st.booleans(),
    blocking_incident_count=st.integers(min_value=-1, max_value=2),
    unresolved_unknown_count=st.integers(min_value=-1, max_value=2),
    blocking_reconciliation_count=st.integers(min_value=-1, max_value=2),
    exposure_effect=st.sampled_from(tuple(ExposureEffect)),
)
def test_exposure_increase_requires_every_hard_gate(
    locally_armed: bool,
    database_writable: bool,
    market_data_fresh: bool,
    active_kill_switch: bool,
    blocking_incident_count: int,
    unresolved_unknown_count: int,
    blocking_reconciliation_count: int,
    exposure_effect: ExposureEffect,
) -> None:
    context = replace(
        approved_increase_context(),
        locally_armed=locally_armed,
        database_writable=database_writable,
        market_data_fresh=market_data_fresh,
        active_kill_switch=active_kill_switch,
        blocking_incident_count=blocking_incident_count,
        unresolved_unknown_count=unresolved_unknown_count,
        blocking_reconciliation_count=blocking_reconciliation_count,
        exposure_effect=exposure_effect,
    )

    decision = SubmissionGate().evaluate(context)

    assert decision.allowed is (
        locally_armed
        and database_writable
        and market_data_fresh
        and not active_kill_switch
        and blocking_incident_count == 0
        and unresolved_unknown_count == 0
        and blocking_reconciliation_count == 0
        and exposure_effect is ExposureEffect.INCREASE
    )
