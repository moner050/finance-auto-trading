"""Putting a secret into use, and taking one out of it.

Section 9 lists secret activation, rotation and retirement among the actions
that need the second password. They are the same shape as arming: the operator
is shown exactly what will change, types the password against that, and the
approval is bound to a digest of what they were shown.

Registering a secret is not on that list, and is not here. Writing a version
nothing uses changes nothing, and asking for a password to store a value that
will need a second confirmation before it does anything would train the
operator to type it twice for one decision.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.auth import Operator
from autotrader.apps.backoffice.ledger import LedgerEntry, MySqlCommandLedger
from autotrader.apps.backoffice.second_password import (
    ApprovalRequest,
    ApprovalStore,
    authority_digest,
)
from autotrader.apps.backoffice.secrets import (
    MySqlSecretStore,
    SecretNotFoundError,
    SecretVersionView,
)
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc

TARGET_TYPE = "SECRET"


class SecretAction(StrEnum):
    ACTIVATE = "ACTIVATE_SECRET"
    RETIRE = "RETIRE_SECRET"


class SecretCommandRefusedError(RuntimeError):
    """Raised when a secret command cannot be carried out as asked."""


@dataclass(frozen=True, slots=True)
class SecretFacts:
    """Exactly what the confirmation panel shows about one secret."""

    logical_name: str
    action: SecretAction
    target_version: int | None
    active_version: int | None
    target_fingerprint: str | None
    active_fingerprint: str | None

    def as_details(self) -> dict[str, object]:
        return {
            "logical_name": self.logical_name,
            "action": self.action.value,
            "target_version": self.target_version,
            "active_version": self.active_version,
            "target_fingerprint": self.target_fingerprint,
            "active_fingerprint": self.active_fingerprint,
        }

    def digest(self) -> bytes:
        return authority_digest(self.as_details())


@dataclass(frozen=True, slots=True)
class SecretCommand:
    id: UUID
    action: SecretAction
    logical_name: str
    target_version: int | None
    operator: Operator
    source_ip: str
    correlation_id: str
    approval_id: str
    requested_at: datetime

    def __post_init__(self) -> None:
        if type(cast(object, self.action)) is not SecretAction:
            raise TypeError("action must be an exact SecretAction")
        if self.id.version != 7:
            raise ValueError("command id must be UUIDv7")
        if not self.approval_id or not self.logical_name:
            raise ValueError("approval_id and logical_name are required")
        if (self.action is SecretAction.ACTIVATE) != (self.target_version is not None):
            # Activation needs to know which version; retirement takes
            # whatever is active, and naming a version there would suggest a
            # choice that does not exist.
            raise ValueError("a version belongs to activation and only to it")
        object.__setattr__(self, "requested_at", require_utc(self.requested_at))


def new_secret_command(
    *,
    action: SecretAction,
    logical_name: str,
    target_version: int | None,
    operator: Operator,
    source_ip: str,
    correlation_id: str,
    approval_id: str,
    requested_at: datetime,
) -> SecretCommand:
    return SecretCommand(
        id=new_uuid7(),
        action=action,
        logical_name=logical_name,
        target_version=target_version,
        operator=operator,
        source_ip=source_ip,
        correlation_id=correlation_id,
        approval_id=approval_id,
        requested_at=requested_at,
    )


def approval_for(
    *, session_id: str, operator: Operator, facts: SecretFacts
) -> ApprovalRequest:
    return ApprovalRequest(
        session_id=session_id,
        operator_email=operator.email,
        action=facts.action.value,
        target_type=TARGET_TYPE,
        target_key=facts.logical_name,
        authority_digest=facts.digest(),
    )


class MySqlSecretCommands:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        store: MySqlSecretStore,
        approvals: ApprovalStore,
        ledger: MySqlCommandLedger | None = None,
    ) -> None:
        self._sessions = sessions
        self._store = store
        self._approvals = approvals
        self._ledger = ledger or MySqlCommandLedger(sessions)

    async def facts(
        self,
        *,
        action: SecretAction,
        logical_name: str,
        target_version: int | None,
    ) -> SecretFacts:
        versions = [
            version
            for version in await self._store.versions()
            if version.logical_name == logical_name
        ]
        if not versions:
            raise SecretNotFoundError(f"nothing is stored under {logical_name}")
        active = next((version for version in versions if version.active), None)
        target = _target(versions, target_version)
        if action is SecretAction.ACTIVATE and target is None:
            raise SecretCommandRefusedError("that version is not stored")
        if action is SecretAction.RETIRE and active is None:
            raise SecretCommandRefusedError("nothing is active to retire")
        return SecretFacts(
            logical_name=logical_name,
            action=action,
            target_version=None if target is None else target.version,
            active_version=None if active is None else active.version,
            target_fingerprint=None if target is None else target.fingerprint,
            active_fingerprint=None if active is None else active.fingerprint,
        )

    async def apply(self, command: SecretCommand, *, session_id: str) -> SecretFacts:
        facts = await self.facts(
            action=command.action,
            logical_name=command.logical_name,
            target_version=command.target_version,
        )
        await self._ledger.open(
            LedgerEntry(
                id=command.id,
                actor_email=command.operator.email,
                source_ip=command.source_ip,
                action=command.action.value,
                target_type=TARGET_TYPE,
                target_key=command.logical_name,
                payload={
                    "action": command.action.value,
                    "correlation_id": command.correlation_id,
                },
                expected_digest=facts.digest(),
                started_at=command.requested_at,
            )
        )
        try:
            await self._approvals.consume(
                command.approval_id,
                approval_for(
                    session_id=session_id, operator=command.operator, facts=facts
                ),
            )
            if command.action is SecretAction.ACTIVATE:
                assert command.target_version is not None
                await self._store.activate(
                    logical_name=command.logical_name,
                    version=command.target_version,
                    now=command.requested_at,
                )
            else:
                await self._store.retire(
                    logical_name=command.logical_name, now=command.requested_at
                )
        except Exception as error:
            await self._ledger.fail(
                command_id=command.id,
                result_code=_failure_code(error),
                completed_at=command.requested_at,
            )
            raise
        # Built rather than re-read: after a retirement there is nothing
        # active, and asking for the facts again would refuse for exactly the
        # reason the command succeeded.
        after = replace(
            facts,
            active_version=(
                command.target_version
                if command.action is SecretAction.ACTIVATE
                else None
            ),
            active_fingerprint=(
                facts.target_fingerprint
                if command.action is SecretAction.ACTIVATE
                else None
            ),
        )
        async with self._sessions() as session:
            await self._ledger.succeed(
                session,
                command_id=command.id,
                result_code="APPLIED",
                result=after.as_details(),
                completed_at=command.requested_at,
            )
            await session.commit()
        return after


def _target(
    versions: list[SecretVersionView], target_version: int | None
) -> SecretVersionView | None:
    if target_version is None:
        return None
    return next(
        (version for version in versions if version.version == target_version), None
    )


_FAILURE_CODES: dict[type[BaseException], str] = {
    SecretNotFoundError: "SECRET_NOT_FOUND",
    SecretCommandRefusedError: "SECRET_COMMAND_REFUSED",
}


def _failure_code(error: BaseException) -> str:
    """A stable code, because a message can be reworded and a grep cannot."""
    return _FAILURE_CODES.get(type(error), "UNEXPECTED_ERROR")


__all__ = (
    "TARGET_TYPE",
    "MySqlSecretCommands",
    "SecretAction",
    "SecretCommand",
    "SecretCommandRefusedError",
    "SecretFacts",
    "approval_for",
    "new_secret_command",
)
