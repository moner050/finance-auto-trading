from __future__ import annotations

from autotrader.config.settings import RuntimeMode
from autotrader.execution.controls.models import (
    ExposureEffect,
    GateAction,
    GateDecision,
    SubmissionContext,
)


class SubmissionGate:
    def evaluate(self, context: SubmissionContext) -> GateDecision:
        reasons = self._authority_failures(context)
        if reasons:
            return GateDecision(allowed=False, reason_codes=tuple(reasons))

        if (
            context.action is GateAction.CANCEL
            and context.exposure_effect is ExposureEffect.REDUCE
        ):
            return GateDecision(allowed=True, reason_codes=())

        reasons.extend(self._increase_failures(context))
        return GateDecision(allowed=not reasons, reason_codes=tuple(reasons))

    @staticmethod
    def _authority_failures(context: SubmissionContext) -> list[str]:
        reasons: list[str] = []
        if not context.database_writable:
            reasons.append("DATABASE_UNWRITABLE")
        lease = context.arm_lease
        if lease is None:
            reasons.append("ARM_LEASE_MISSING")
        else:
            if lease.expires_at <= context.now:
                reasons.append("ARM_LEASE_EXPIRED")
            if lease.owner_runtime_instance_id != context.local_runtime_instance_id:
                reasons.append("ARM_LEASE_NOT_OWNED")
            if (
                lease.acquired_at > context.now
                or lease.fencing_token <= 0
                or lease.row_version <= 0
            ):
                reasons.append("ARM_LEASE_INVALID")
        return reasons

    @staticmethod
    def _increase_failures(context: SubmissionContext) -> list[str]:
        reasons: list[str] = []
        if not context.locally_armed:
            reasons.append("LOCAL_DISARMED")
        if context.runtime_mode is RuntimeMode.LIVE:
            if not context.allow_live:
                reasons.append("LIVE_NOT_ALLOWED")
            if context.account_environment != RuntimeMode.LIVE:
                reasons.append("ACCOUNT_ENVIRONMENT_MISMATCH")
        elif context.runtime_mode is RuntimeMode.PAPER:
            if context.account_environment != RuntimeMode.PAPER:
                reasons.append("ACCOUNT_ENVIRONMENT_MISMATCH")
        else:
            reasons.append("RUNTIME_MODE_DENIED")
        if not context.market_data_fresh:
            reasons.append("MARKET_DATA_STALE")
        if context.active_kill_switch:
            reasons.append("KILL_SWITCH_ACTIVE")
        if context.blocking_incident_count < 0:
            reasons.append("BLOCKING_INCIDENT_STATE_INVALID")
        elif context.blocking_incident_count > 0:
            reasons.append("BLOCKING_INCIDENT_ACTIVE")
        if context.unresolved_unknown_count < 0:
            reasons.append("UNKNOWN_ORDER_STATE_INVALID")
        elif context.unresolved_unknown_count > 0:
            reasons.append("UNKNOWN_ORDER_ACTIVE")
        if context.blocking_reconciliation_count < 0:
            reasons.append("RECONCILIATION_STATE_INVALID")
        elif context.blocking_reconciliation_count > 0:
            reasons.append("RECONCILIATION_BLOCKING")
        if context.exposure_effect is ExposureEffect.UNKNOWN:
            reasons.append("EXPOSURE_EFFECT_UNKNOWN")
        elif context.exposure_effect is not ExposureEffect.INCREASE:
            reasons.append("EXPOSURE_EFFECT_NOT_ALLOWED")
        return reasons
