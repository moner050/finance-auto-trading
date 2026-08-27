"""Starting, which is a different kind of control from stopping.

Stopping is always available and asks for nothing but a session and a form
token. Starting asks for the password again, and the approval that produces is
bound to a digest of exactly what the operator was shown. The digest is
recomputed under the same lock the change will use, so an approval issued
against one account, policy and control state cannot be spent against another.

Clearing a halt is separate from arming on purpose. One click that both undoes
an emergency and starts trading again is one click too few for either.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.auth import Operator
from autotrader.apps.backoffice.commands import (
    NO_KILL_SWITCH,
    ControlOutcome,
    ControlState,
    NothingToControlError,
    control_state,
)
from autotrader.apps.backoffice.second_password import (
    ApprovalRequest,
    ApprovalStore,
    authority_digest,
)
from autotrader.persistence.mysql.models.accounts import Account, Broker
from autotrader.persistence.mysql.models.operations import (
    OpsAuditLog,
    OpsTradingControl,
)
from autotrader.persistence.mysql.models.risk import RiskPolicyVersion
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc

TARGET_TYPE = "GLOBAL"
TARGET_KEY = "ALL"


class DangerousAction(StrEnum):
    CLEAR_HALT = "CLEAR_HALT"
    ARM = "ARM"


class StillHaltedError(RuntimeError):
    """Raised when arming is attempted while a kill switch is down."""


@dataclass(frozen=True, slots=True)
class ArmingFacts:
    """Exactly what the confirmation panel shows, and what binds the approval.

    Section 12 also asks for the universe and a readiness digest. Neither
    exists on this branch, and naming a field for something nothing computes
    would put a reassuring blank on a panel whose whole job is to be exact.
    """

    account_alias: str
    broker_code: str
    environment: str
    policy_version: str | None
    armed: bool
    kill_switch_level: str

    def as_details(self) -> dict[str, object]:
        return {
            "account_alias": self.account_alias,
            "broker_code": self.broker_code,
            "environment": self.environment,
            "policy_version": self.policy_version,
            "armed": self.armed,
            "kill_switch_level": self.kill_switch_level,
        }

    def digest(self) -> bytes:
        return authority_digest(self.as_details())


@dataclass(frozen=True, slots=True)
class ExposureCommand:
    """A dangerous action, and the approval that lets it happen."""

    id: UUID
    action: DangerousAction
    operator: Operator
    source_ip: str | None
    correlation_id: str
    approval_id: str
    requested_at: datetime

    def __post_init__(self) -> None:
        if type(cast(object, self.action)) is not DangerousAction:
            raise TypeError("action must be an exact DangerousAction")
        if self.id.version != 7:
            raise ValueError("command id must be UUIDv7")
        if not self.approval_id:
            raise ValueError("approval_id is required")
        object.__setattr__(self, "requested_at", require_utc(self.requested_at))


def new_exposure_command(
    *,
    action: DangerousAction,
    operator: Operator,
    source_ip: str | None,
    correlation_id: str,
    approval_id: str,
    requested_at: datetime,
) -> ExposureCommand:
    return ExposureCommand(
        id=new_uuid7(),
        action=action,
        operator=operator,
        source_ip=source_ip,
        correlation_id=correlation_id,
        approval_id=approval_id,
        requested_at=requested_at,
    )


def approval_for(
    *, session_id: str, operator: Operator, action: DangerousAction, facts: ArmingFacts
) -> ApprovalRequest:
    return ApprovalRequest(
        session_id=session_id,
        operator_email=operator.email,
        action=action.value,
        target_type=TARGET_TYPE,
        target_key=TARGET_KEY,
        authority_digest=facts.digest(),
    )


class MySqlExposureControls:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        approvals: ApprovalStore,
        account_id: UUID,
    ) -> None:
        self._sessions = sessions
        self._approvals = approvals
        self._account_id = account_id

    async def facts(self) -> ArmingFacts:
        async with self._sessions() as session:
            return await _arming_facts(session, self._account_id)

    async def apply(
        self, command: ExposureCommand, *, session_id: str
    ) -> ControlOutcome:
        async with self._sessions() as session:
            controls = list(
                (
                    await session.scalars(select(OpsTradingControl).with_for_update())
                ).all()
            )
            if not controls:
                raise NothingToControlError("there are no controls to act on")
            facts = await _arming_facts(session, self._account_id, controls=controls)
            await self._approvals.consume(
                command.approval_id,
                approval_for(
                    session_id=session_id,
                    operator=command.operator,
                    action=command.action,
                    facts=facts,
                ),
            )
            before = control_state(controls)
            if command.action is DangerousAction.ARM:
                if before.kill_switch_level != NO_KILL_SWITCH:
                    # Arming through a halt would make the halt advisory.
                    raise StillHaltedError("clear the halt before arming")
                for control in controls:
                    control.armed = True
                    control.row_version += 1
            else:
                for control in controls:
                    control.kill_switch_level = NO_KILL_SWITCH
                    control.row_version += 1
            after = control_state(controls)
            session.add(
                OpsAuditLog(
                    id=new_uuid7(),
                    action=f"BACKOFFICE_{command.action.value}",
                    scope_type=TARGET_TYPE,
                    scope_key=TARGET_KEY,
                    actor_runtime_instance_id=None,
                    fencing_token=max(control.fencing_token for control in controls),
                    details={
                        "command_id": str(command.id),
                        "operator_email": command.operator.email,
                        "source_ip": command.source_ip,
                        "correlation_id": command.correlation_id,
                        "second_password_verified": True,
                        "authority_digest": facts.digest().hex(),
                        "before": before.as_details(),
                        "after": after.as_details(),
                    },
                    occurred_at=command.requested_at,
                )
            )
            await session.commit()
        return ControlOutcome(
            command_id=command.id,
            action=command.action.value,
            armed=after.armed,
            kill_switch_level=after.kill_switch_level,
            repeated=False,
        )


async def _arming_facts(
    session: AsyncSession,
    account_id: UUID,
    *,
    controls: list[OpsTradingControl] | None = None,
) -> ArmingFacts:
    account = await session.get(Account, account_id)
    if account is None:
        raise NothingToControlError("the account named for arming does not exist")
    broker = await session.get(Broker, account.broker_id)
    policy_version = await session.scalar(
        select(RiskPolicyVersion.version)
        .where(RiskPolicyVersion.active.is_(True))
        .order_by(RiskPolicyVersion.id.desc())
        .limit(1)
    )
    rows = (
        controls
        if controls is not None
        else list((await session.scalars(select(OpsTradingControl))).all())
    )
    state = control_state(rows) if rows else ControlState(False, NO_KILL_SWITCH)
    return ArmingFacts(
        account_alias=account.account_alias,
        # Naming the broker matters more than hiding a gap: an account with no
        # broker row is a configuration fault an operator should see on the
        # panel rather than discover afterwards.
        broker_code="UNKNOWN" if broker is None else broker.code,
        environment=account.environment,
        policy_version=policy_version,
        armed=state.armed,
        kill_switch_level=state.kill_switch_level,
    )


__all__ = (
    "TARGET_KEY",
    "TARGET_TYPE",
    "ArmingFacts",
    "DangerousAction",
    "ExposureCommand",
    "MySqlExposureControls",
    "StillHaltedError",
    "approval_for",
    "new_exposure_command",
)
