"""Creating an account, turning it on, and binding a provider to it.

Section 9 names two of these as needing the second password — account
enablement, and binding activation or replacement — and does not name the
third. Creating an account writes a row that cannot trade: it is disabled, and
enabling it is the gated step. Putting a password on creation as well would
add a prompt with no decision behind it and blur which step is the gate.

Disabling is not gated either, for the same reason HALT is not: a control that
takes an account out of service must work when the thing that would verify a
password is the thing that is broken.
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
from autotrader.persistence.mysql.models.accounts import Account, Broker
from autotrader.persistence.mysql.repositories.accounts import AccountRepository
from autotrader.persistence.mysql.repositories.provider_binding import (
    PROVIDERS,
    ProviderBindingRefusedError,
    ProviderBindings,
    require_sequence_rule,
)
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc

# KIS offers both; Toss and Binance are live-only, and the schema's account
# constraint is what says so.
ENVIRONMENTS = frozenset({"PAPER", "LIVE"})

TARGET_TYPE = "ACCOUNT"
BINDING_TARGET_TYPE = "PROVIDER_BINDING"
CREATE = "CREATE_ACCOUNT"
ENABLE = "ENABLE_ACCOUNT"
DISABLE = "DISABLE_ACCOUNT"
BIND_PROVIDER = "BIND_PROVIDER"


class AccountCommandRefusedError(RuntimeError):
    """Raised when an account cannot be changed as asked."""


@dataclass(frozen=True, slots=True)
class EnableFacts:
    """What the panel shows before an account is put into service."""

    account_id: UUID
    account_alias: str
    environment: str
    broker_code: str
    enabled: bool
    provider_bound: bool

    def as_details(self) -> dict[str, object]:
        return {
            "account_id": str(self.account_id),
            "account_alias": self.account_alias,
            "environment": self.environment,
            "broker_code": self.broker_code,
            "enabled": self.enabled,
            "provider_bound": self.provider_bound,
        }

    def digest(self) -> bytes:
        return authority_digest(self.as_details())


@dataclass(frozen=True, slots=True)
class ProviderBindingFacts:
    account_id: UUID
    account_alias: str
    provider_code: str
    environment: str
    account_seq: int | None
    current_revision: int | None

    def as_details(self) -> dict[str, object]:
        return {
            "account_id": str(self.account_id),
            "account_alias": self.account_alias,
            "provider_code": self.provider_code,
            "environment": self.environment,
            "account_seq": self.account_seq,
            "current_revision": self.current_revision,
        }

    def digest(self) -> bytes:
        return authority_digest(self.as_details())


def enable_approval_for(
    *, session_id: str, operator: Operator, facts: EnableFacts
) -> ApprovalRequest:
    return ApprovalRequest(
        session_id=session_id,
        operator_email=operator.email,
        action=ENABLE,
        target_type=TARGET_TYPE,
        target_key=facts.account_alias,
        authority_digest=facts.digest(),
    )


def binding_approval_for(
    *, session_id: str, operator: Operator, facts: ProviderBindingFacts
) -> ApprovalRequest:
    return ApprovalRequest(
        session_id=session_id,
        operator_email=operator.email,
        action=BIND_PROVIDER,
        target_type=BINDING_TARGET_TYPE,
        target_key=f"{facts.account_alias}:{facts.provider_code}",
        authority_digest=facts.digest(),
    )


class MySqlAccountCommands:
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

    async def create(
        self,
        *,
        broker_code: str,
        account_alias: str,
        environment: str,
        secret_reference: str,
        operator: Operator,
        source_ip: str,
        correlation_id: str,
        now: datetime,
    ) -> UUID:
        """A new account, disabled. Enabling it is the gated step."""
        moment = require_utc(now)
        command_id = new_uuid7()
        await self._ledger.open(
            LedgerEntry(
                id=command_id,
                actor_email=operator.email,
                source_ip=source_ip,
                action=CREATE,
                target_type=TARGET_TYPE,
                target_key=account_alias,
                payload={
                    "correlation_id": correlation_id,
                    "broker": broker_code,
                    "environment": environment,
                },
                expected_digest=None,
                started_at=moment,
            )
        )
        try:
            async with self._sessions() as session:
                broker = await session.scalar(
                    select(Broker).where(Broker.code == broker_code)
                )
                if broker is None:
                    raise AccountCommandRefusedError("저장되지 않은 브로커입니다.")
                if environment not in ENVIRONMENTS:
                    raise AccountCommandRefusedError(
                        "환경은 PAPER 또는 LIVE 여야 합니다."
                    )
                account = await AccountRepository(session).create(
                    broker_id=broker.id,
                    account_alias=account_alias,
                    environment=environment,
                    secret_reference=secret_reference,
                    # Disabled. Section 9 gates enablement, and a create that
                    # enabled would hand out the gated state without the gate.
                    enabled=False,
                )
                account_id = account.id
                await self._ledger.succeed(
                    session,
                    command_id=command_id,
                    result_code="CREATED",
                    result={
                        "account_id": str(account_id),
                        "environment": environment,
                        "enabled": False,
                    },
                    completed_at=moment,
                )
                await session.commit()
        except Exception as error:
            await self._ledger.fail(
                command_id=command_id,
                result_code=_failure_code(error),
                completed_at=moment,
            )
            raise
        return account_id

    async def enable_facts(self, account_id: UUID) -> EnableFacts:
        async with self._sessions() as session:
            found = (
                await session.execute(
                    select(Account, Broker.code)
                    .join(Broker, Broker.id == Account.broker_id)
                    .where(Account.id == account_id)
                )
            ).first()
            if found is None:
                raise AccountCommandRefusedError("저장되지 않은 계좌입니다.")
            account, broker_code = found
            bound = await ProviderBindings(session).active_for(
                account_id, provider_code=broker_code
            )
            facts = EnableFacts(
                account_id=account_id,
                account_alias=account.account_alias,
                environment=account.environment,
                broker_code=broker_code,
                enabled=account.enabled,
                provider_bound=bound is not None,
            )
            await session.rollback()
        return facts

    async def set_enabled(
        self,
        *,
        account_id: UUID,
        enabled: bool,
        operator: Operator,
        source_ip: str,
        correlation_id: str,
        approval_id: str | None,
        session_id: str,
        now: datetime,
    ) -> EnableFacts:
        """Turn an account on, or take it out of service.

        Only turning it on consumes an approval. Disabling has to work when
        the approval path does not, for the same reason HALT does.
        """
        moment = require_utc(now)
        facts = await self.enable_facts(account_id)
        if facts.enabled == enabled:
            raise AccountCommandRefusedError("이미 그 상태입니다.")
        if enabled and not facts.provider_bound:
            # An enabled account with no provider bound is one the loop will
            # pick up and then fail to act for.
            raise AccountCommandRefusedError(
                "provider 바인딩이 없는 계좌는 사용할 수 없습니다."
            )
        command_id = new_uuid7()
        action = ENABLE if enabled else DISABLE
        await self._ledger.open(
            LedgerEntry(
                id=command_id,
                actor_email=operator.email,
                source_ip=source_ip,
                action=action,
                target_type=TARGET_TYPE,
                target_key=facts.account_alias,
                payload={"correlation_id": correlation_id},
                expected_digest=facts.digest(),
                started_at=moment,
            )
        )
        try:
            if enabled:
                if approval_id is None:
                    raise AccountCommandRefusedError("2차 비밀번호 승인이 필요합니다.")
                await self._approvals.consume(
                    approval_id,
                    enable_approval_for(
                        session_id=session_id, operator=operator, facts=facts
                    ),
                )
            async with self._sessions() as session:
                await AccountRepository(session).set_enabled(
                    account_id, enabled=enabled
                )
                await self._ledger.succeed(
                    session,
                    command_id=command_id,
                    result_code="ENABLED" if enabled else "DISABLED",
                    result=facts.as_details(),
                    completed_at=moment,
                )
                await session.commit()
        except Exception as error:
            await self._ledger.fail(
                command_id=command_id,
                result_code=_failure_code(error),
                completed_at=moment,
            )
            raise
        return facts

    async def binding_facts(
        self, *, account_id: UUID, provider_code: str, account_seq: int | None
    ) -> ProviderBindingFacts:
        """What the panel shows, refusing anything the bind would refuse.

        Checked here and not only in the repository. `bind` held the
        account_seq rule alone, so a BINANCE binding carrying one passed the
        panel, took the operator's second password, and was refused afterwards
        - spending an approval on a change that could never happen and
        reporting it as a 500. Section 11.4's policy binding already says why:
        learning after the fact that it was never bindable tells the operator
        nothing they could not have been told first.
        """
        if provider_code not in PROVIDERS:
            raise ProviderBindingRefusedError("승인된 provider가 아닙니다.")
        require_sequence_rule(provider_code, account_seq)
        async with self._sessions() as session:
            account = await session.scalar(
                select(Account).where(Account.id == account_id)
            )
            if account is None:
                raise AccountCommandRefusedError("저장되지 않은 계좌입니다.")
            current = await ProviderBindings(session).active_for(
                account_id, provider_code=provider_code
            )
            facts = ProviderBindingFacts(
                account_id=account_id,
                account_alias=account.account_alias,
                provider_code=provider_code,
                environment=account.environment,
                account_seq=account_seq,
                current_revision=None if current is None else current.revision,
            )
            await session.rollback()
        return facts

    async def bind_provider(
        self,
        *,
        account_id: UUID,
        provider_code: str,
        account_seq: int | None,
        operator: Operator,
        source_ip: str,
        correlation_id: str,
        approval_id: str,
        session_id: str,
        now: datetime,
    ) -> ProviderBindingFacts:
        moment = require_utc(now)
        facts = await self.binding_facts(
            account_id=account_id,
            provider_code=provider_code,
            account_seq=account_seq,
        )
        command_id = new_uuid7()
        await self._ledger.open(
            LedgerEntry(
                id=command_id,
                actor_email=operator.email,
                source_ip=source_ip,
                action=BIND_PROVIDER,
                target_type=BINDING_TARGET_TYPE,
                target_key=f"{facts.account_alias}:{provider_code}",
                payload={"correlation_id": correlation_id},
                expected_digest=facts.digest(),
                started_at=moment,
            )
        )
        try:
            await self._approvals.consume(
                approval_id,
                binding_approval_for(
                    session_id=session_id, operator=operator, facts=facts
                ),
            )
            async with self._sessions() as session:
                binding = await ProviderBindings(session).bind(
                    account_id=account_id,
                    provider_code=provider_code,
                    account_seq=account_seq,
                    now=moment,
                )
                await self._ledger.succeed(
                    session,
                    command_id=command_id,
                    result_code="BOUND",
                    result={**facts.as_details(), "revision": binding.revision},
                    completed_at=moment,
                )
                await session.commit()
        except Exception as error:
            await self._ledger.fail(
                command_id=command_id,
                result_code=_failure_code(error),
                completed_at=moment,
            )
            raise
        return facts


_FAILURE_CODES: dict[type[BaseException], str] = {
    AccountCommandRefusedError: "ACCOUNT_COMMAND_REFUSED",
    ProviderBindingRefusedError: "PROVIDER_BINDING_REFUSED",
}


def _failure_code(error: BaseException) -> str:
    """A stable code, because a message can be reworded and a grep cannot."""
    return _FAILURE_CODES.get(type(error), "UNEXPECTED_ERROR")


__all__ = (
    "BIND_PROVIDER",
    "CREATE",
    "DISABLE",
    "ENABLE",
    "AccountCommandRefusedError",
    "EnableFacts",
    "MySqlAccountCommands",
    "ProviderBindingFacts",
    "binding_approval_for",
    "enable_approval_for",
)
