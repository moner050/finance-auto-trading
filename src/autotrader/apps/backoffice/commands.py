"""The safety controls, as commands rather than as database writes.

Section 12 is the constraint that shapes this file: HALT and DISARM require
authentication and a form token, and nothing else. Not readiness, not provider
availability, not a decrypted secret, not a second password. A safety control
that depends on the rest of the system working is not a safety control, since
the moment you need it is the moment the rest of the system is not working.

So the handler here reads and writes exactly two things: the control rows and
the audit record, in one transaction. It asks nobody whether now is a good
time.
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
from autotrader.apps.backoffice.ledger import LedgerEntry, MySqlCommandLedger
from autotrader.persistence.mysql.models.operations import (
    OpsAuditLog,
    OpsTradingControl,
)
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc

TARGET_TYPE = "GLOBAL"
TARGET_KEY = "ALL"
NO_KILL_SWITCH = "NONE"
BLOCK_NEW_EXPOSURE = "BLOCK_NEW_EXPOSURE"
EMERGENCY = "EMERGENCY"


class SafetyAction(StrEnum):
    """What an operator can ask for, and nothing that opens exposure.

    Arming is deliberately absent. Section 12 puts exposure-enabling actions
    behind a confirmation panel naming the account, policy and readiness
    digest, and that is a different kind of control from a button whose whole
    point is that it always works.
    """

    DISARM = "DISARM"
    HALT = "HALT"
    EMERGENCY = "EMERGENCY"


_KILL_SWITCH = {
    SafetyAction.DISARM: None,
    SafetyAction.HALT: BLOCK_NEW_EXPOSURE,
    SafetyAction.EMERGENCY: EMERGENCY,
}


class NothingToControlError(RuntimeError):
    """Raised when there are no control rows for a command to act on."""


@dataclass(frozen=True, slots=True)
class SafetyCommand:
    """One request, identified so a resubmitted form is not a second command."""

    id: UUID
    action: SafetyAction
    operator: Operator
    source_ip: str
    correlation_id: str
    requested_at: datetime

    def __post_init__(self) -> None:
        if type(cast(object, self.action)) is not SafetyAction:
            raise TypeError("action must be an exact SafetyAction")
        if self.id.version != 7:
            raise ValueError("command id must be UUIDv7")
        object.__setattr__(self, "requested_at", require_utc(self.requested_at))


@dataclass(frozen=True, slots=True)
class ControlState:
    """What the controls say, collapsed to the two facts that matter."""

    armed: bool
    kill_switch_level: str

    def as_details(self) -> dict[str, object]:
        return {"armed": self.armed, "kill_switch_level": self.kill_switch_level}


@dataclass(frozen=True, slots=True)
class ControlOutcome:
    """What the command did, read back from what was committed."""

    command_id: UUID
    action: str
    armed: bool
    kill_switch_level: str


def new_command(
    *,
    action: SafetyAction,
    operator: Operator,
    source_ip: str,
    correlation_id: str,
    requested_at: datetime,
) -> SafetyCommand:
    return SafetyCommand(
        id=new_uuid7(),
        action=action,
        operator=operator,
        source_ip=source_ip,
        correlation_id=correlation_id,
        requested_at=requested_at,
    )


class MySqlSafetyControls:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        ledger: MySqlCommandLedger | None = None,
    ) -> None:
        self._sessions = sessions
        self._ledger = ledger or MySqlCommandLedger(sessions)

    async def apply(self, command: SafetyCommand) -> ControlOutcome:
        # Opened and committed before anything is attempted, so a command that
        # dies halfway leaves a row saying it was tried.
        await self._ledger.open(
            LedgerEntry(
                id=command.id,
                actor_email=command.operator.email,
                source_ip=command.source_ip,
                action=command.action.value,
                target_type=TARGET_TYPE,
                target_key=TARGET_KEY,
                payload={
                    "action": command.action.value,
                    "correlation_id": command.correlation_id,
                },
                expected_digest=None,
                started_at=command.requested_at,
            )
        )
        try:
            return await self._apply(command)
        except Exception as error:
            await self._ledger.fail(
                command_id=command.id,
                result_code=failure_code(error),
                completed_at=command.requested_at,
            )
            raise

    async def _apply(self, command: SafetyCommand) -> ControlOutcome:
        async with self._sessions() as session:
            controls = list(
                (
                    await session.scalars(select(OpsTradingControl).with_for_update())
                ).all()
            )
            if not controls:
                # Refusing is the honest answer. Writing a control row here
                # would invent a scope nobody configured, and reporting
                # success would tell an operator the system is stopped when
                # nothing was ever running under this name.
                raise NothingToControlError("there are no controls to act on")
            before = control_state(controls)
            for control in controls:
                _apply_to(control, command.action)
                control.row_version += 1
            after = control_state(controls)
            session.add(
                OpsAuditLog(
                    id=new_uuid7(),
                    action=f"BACKOFFICE_{command.action.value}",
                    scope_type=TARGET_TYPE,
                    scope_key=TARGET_KEY,
                    actor_runtime_instance_id=None,
                    # Which runtime generation this was applied against.
                    fencing_token=max(control.fencing_token for control in controls),
                    details=_details(command, before=before, after=after),
                    occurred_at=command.requested_at,
                )
            )
            await self._ledger.succeed(
                session,
                command_id=command.id,
                result_code="APPLIED",
                result=after.as_details(),
                completed_at=command.requested_at,
            )
            # The state change, the control history and the command record all
            # commit together. An unrecorded halt is indistinguishable from one
            # that never happened.
            await session.commit()
        return ControlOutcome(
            command_id=command.id,
            action=command.action.value,
            armed=after.armed,
            kill_switch_level=after.kill_switch_level,
        )


def failure_code(error: BaseException) -> str:
    """A stable code for the ledger, rather than a message that may change."""
    return _FAILURE_CODES.get(type(error), "UNEXPECTED_ERROR")


def _apply_to(control: OpsTradingControl, action: SafetyAction) -> None:
    """Every action disarms. A halt that left the system armed would be a
    label rather than a control."""
    control.armed = False
    level = _KILL_SWITCH[action]
    if level is not None:
        control.kill_switch_level = level


def control_state(controls: list[OpsTradingControl]) -> ControlState:
    return ControlState(
        armed=all(control.armed for control in controls),
        kill_switch_level=_strongest(controls),
    )


def _strongest(controls: list[OpsTradingControl]) -> str:
    levels = {control.kill_switch_level for control in controls}
    for level in (EMERGENCY, BLOCK_NEW_EXPOSURE):
        if level in levels:
            return level
    return NO_KILL_SWITCH


def _details(
    command: SafetyCommand,
    *,
    before: ControlState,
    after: ControlState,
) -> dict[str, object]:
    """What the audit record carries.

    The operator's email is required by the audit contract. Everything a
    secret could ride in on is absent by construction: this dictionary is
    built here from named fields rather than assembled from a request.
    """
    return {
        "command_id": str(command.id),
        "operator_email": command.operator.email,
        "source_ip": command.source_ip,
        "correlation_id": command.correlation_id,
        "before": before.as_details(),
        "after": after.as_details(),
    }


_FAILURE_CODES: dict[type[BaseException], str] = {
    NothingToControlError: "NOTHING_TO_CONTROL",
}


__all__ = (
    "BLOCK_NEW_EXPOSURE",
    "EMERGENCY",
    "NO_KILL_SWITCH",
    "TARGET_KEY",
    "TARGET_TYPE",
    "ControlOutcome",
    "ControlState",
    "MySqlSafetyControls",
    "NothingToControlError",
    "SafetyAction",
    "SafetyCommand",
    "control_state",
    "failure_code",
    "new_command",
)
