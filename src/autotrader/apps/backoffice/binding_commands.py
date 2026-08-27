"""Binding an account to a risk policy version.

Section 11.4's fourth operation. It sits behind the second password for the
same reason activation does: the binding decides which fractions size that
account's trades, and the loop reads it on the next evaluation.

What the panel shows is the account, the version it leaves, and the version it
arrives at. The scope is not shown as a choice because it is not one — it
comes from the approved definition behind the version, and the GUI may not
broaden a market scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.auth import Operator
from autotrader.apps.backoffice.ledger import LedgerEntry, MySqlCommandLedger
from autotrader.apps.backoffice.second_password import (
    ApprovalRequest,
    ApprovalStore,
    authority_digest,
)
from autotrader.persistence.mysql.models.accounts import Account
from autotrader.persistence.mysql.models.risk import RiskPolicy, RiskPolicyVersion
from autotrader.persistence.mysql.repositories.david_v6_risk import (
    V6RiskPolicyDefinition,
    approved_definition,
    policy_row_refusal,
)
from autotrader.persistence.mysql.repositories.policy_binding import (
    AccountPolicyBindings,
    BindingRefusedError,
    PolicyBinding,
)
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc

TARGET_TYPE = "ACCOUNT_POLICY_BINDING"
BIND = "BIND_ACCOUNT_RISK_POLICY"


@dataclass(frozen=True, slots=True)
class BindingFacts:
    """What the panel shows before the password is typed."""

    account_id: UUID
    account_alias: str
    environment: str
    target_version_id: UUID
    target_policy_code: str
    target_version: str
    target_scope: str
    current_version: str | None
    current_policy_code: str | None

    def as_details(self) -> dict[str, object]:
        return {
            "account_id": str(self.account_id),
            "account_alias": self.account_alias,
            "environment": self.environment,
            "target_version_id": str(self.target_version_id),
            "target_policy_code": self.target_policy_code,
            "target_version": self.target_version,
            "target_scope": self.target_scope,
            "current_version": self.current_version,
            "current_policy_code": self.current_policy_code,
        }

    def digest(self) -> bytes:
        return authority_digest(self.as_details())


@dataclass(frozen=True, slots=True)
class BindingCommand:
    id: UUID
    account_id: UUID
    target_version_id: UUID
    operator: Operator
    source_ip: str
    correlation_id: str
    approval_id: str
    requested_at: datetime

    def __post_init__(self) -> None:
        if self.id.version != 7:
            raise ValueError("command id must be UUIDv7")
        if not self.approval_id:
            raise ValueError("approval_id is required")
        object.__setattr__(self, "requested_at", require_utc(self.requested_at))


def new_binding_command(
    *,
    account_id: UUID,
    target_version_id: UUID,
    operator: Operator,
    source_ip: str,
    correlation_id: str,
    approval_id: str,
    requested_at: datetime,
) -> BindingCommand:
    return BindingCommand(
        id=new_uuid7(),
        account_id=account_id,
        target_version_id=target_version_id,
        operator=operator,
        source_ip=source_ip,
        correlation_id=correlation_id,
        approval_id=approval_id,
        requested_at=requested_at,
    )


def approval_for(
    *, session_id: str, operator: Operator, facts: BindingFacts
) -> ApprovalRequest:
    return ApprovalRequest(
        session_id=session_id,
        operator_email=operator.email,
        action=BIND,
        target_type=TARGET_TYPE,
        target_key=f"{facts.account_alias}:{facts.target_version}",
        authority_digest=facts.digest(),
    )


class MySqlBindingCommands:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        approvals: ApprovalStore,
        ledger: MySqlCommandLedger | None = None,
    ) -> None:
        self._sessions = sessions
        self._approvals = approvals
        self._ledger = ledger or MySqlCommandLedger(sessions)

    async def facts(self, *, account_id: UUID, target_version_id: UUID) -> BindingFacts:
        async with self._sessions() as session:
            account = await session.scalar(
                select(Account).where(Account.id == account_id)
            )
            if account is None:
                raise BindingRefusedError("저장되지 않은 계좌입니다.")
            found = (
                await session.execute(
                    select(RiskPolicyVersion, RiskPolicy.code)
                    .join(RiskPolicy, RiskPolicy.id == RiskPolicyVersion.policy_id)
                    .where(RiskPolicyVersion.id == target_version_id)
                )
            ).first()
            if found is None:
                raise BindingRefusedError("저장되지 않은 정책 버전입니다.")
            version, code = found
            refusal = policy_row_refusal(version, code=code)
            if refusal is not None:
                raise BindingRefusedError(refusal)
            if not version.active:
                # Refuse before the password is typed. Learning after the fact
                # that the version was never bindable wastes an approval and
                # tells the operator nothing they could not have been told.
                raise BindingRefusedError("적용 중인 버전만 계좌에 연결할 수 있습니다.")
            current = await AccountPolicyBindings(session).active_binding(account_id)
            # Read every column before the session goes away. A rollback
            # expires the instances, and a detached one raises on attribute
            # access rather than returning what it last held.
            alias = account.account_alias
            environment = account.environment
            version_name = version.version
            await session.rollback()

        definition = _definition_or_refuse(code)
        return BindingFacts(
            account_id=account_id,
            account_alias=alias,
            environment=environment,
            target_version_id=target_version_id,
            target_policy_code=code,
            target_version=version_name,
            target_scope=definition.currency or definition.settlement_asset or "-",
            current_version=None if current is None else current.version,
            current_policy_code=None if current is None else current.policy_code,
        )

    async def bind(self, command: BindingCommand, *, session_id: str) -> PolicyBinding:
        facts = await self.facts(
            account_id=command.account_id,
            target_version_id=command.target_version_id,
        )
        await self._ledger.open(
            LedgerEntry(
                id=command.id,
                actor_email=command.operator.email,
                source_ip=command.source_ip,
                action=BIND,
                target_type=TARGET_TYPE,
                target_key=f"{facts.account_alias}:{facts.target_version}",
                payload={"correlation_id": command.correlation_id},
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
            async with self._sessions() as session:
                binding = await AccountPolicyBindings(session).bind(
                    account_id=command.account_id,
                    policy_version_id=command.target_version_id,
                    now=command.requested_at,
                )
                await self._ledger.succeed(
                    session,
                    command_id=command.id,
                    result_code="BOUND",
                    result=facts.as_details(),
                    completed_at=command.requested_at,
                )
                # The binding that takes effect and the record that it did
                # commit together.
                await session.commit()
        except Exception as error:
            await self._ledger.fail(
                command_id=command.id,
                result_code=_failure_code(error),
                completed_at=command.requested_at,
            )
            raise
        return binding


def _definition_or_refuse(code: str) -> V6RiskPolicyDefinition:
    definition = approved_definition(code)
    if definition is None:
        raise BindingRefusedError("이 정책 코드는 승인된 v6 정책이 아닙니다.")
    return definition


_FAILURE_CODES: dict[type[BaseException], str] = {
    BindingRefusedError: "BINDING_REFUSED",
}


def _failure_code(error: BaseException) -> str:
    """A stable code, because a message can be reworded and a grep cannot."""
    return _FAILURE_CODES.get(type(error), "UNEXPECTED_ERROR")


__all__ = (
    "BIND",
    "TARGET_TYPE",
    "BindingCommand",
    "BindingFacts",
    "MySqlBindingCommands",
    "approval_for",
    "new_binding_command",
)
