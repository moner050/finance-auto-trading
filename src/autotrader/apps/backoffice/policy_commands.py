"""Putting a risk policy version into force.

Section 9 lists risk-policy activation among the actions that need the second
password, and it is the one where the reason is easiest to state: these
fractions decide how much money each trade puts at risk, and the engine reads
them on the next evaluation.

So the panel shows the difference rather than the new version alone. An
operator confirming a change they cannot see is confirming a number, and the
number that matters is the one that moved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.auth import Operator
from autotrader.apps.backoffice.ledger import LedgerEntry, MySqlCommandLedger
from autotrader.apps.backoffice.policies_read_model import (
    PoliciesReadModel,
    PolicyDifference,
    PolicyVersionView,
    as_percentage,
    difference,
)
from autotrader.apps.backoffice.second_password import (
    ApprovalRequest,
    ApprovalStore,
    authority_digest,
)
from autotrader.persistence.mysql.models.risk import RiskPolicy, RiskPolicyVersion
from autotrader.persistence.mysql.repositories.david_v6_risk import (
    APPROVED_V6_RISK_POLICIES,
    V6RiskPolicyDefinition,
    approved_definition,
    policy_row_refusal,
    policy_row_values,
)
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc

TARGET_TYPE = "RISK_POLICY"
ACTIVATE = "ACTIVATE_RISK_POLICY"
CREATE = "CREATE_RISK_POLICY_VERSION"


class PolicyCommandRefusedError(RuntimeError):
    """Raised when a policy cannot be put into force as asked."""


@dataclass(frozen=True, slots=True)
class PolicyFacts:
    """What the panel shows: the two versions, and every field between them."""

    policy_code: str
    target_version: str
    target_version_id: UUID
    active_version: str | None
    differences: tuple[PolicyDifference, ...]

    def as_details(self) -> dict[str, object]:
        return {
            "policy_code": self.policy_code,
            "target_version": self.target_version,
            "target_version_id": str(self.target_version_id),
            "active_version": self.active_version,
            "differences": [
                {"name": item.name, "left": item.left, "right": item.right}
                for item in self.differences
            ],
        }

    def digest(self) -> bytes:
        return authority_digest(self.as_details())


@dataclass(frozen=True, slots=True)
class CreatableVersion:
    """An approved definition with no stored row yet.

    Creation is a materialisation, not an entry form. The operator picks a
    definition and the row is written from it, so a created version is loadable
    by construction rather than by inspection. There is no field to mistype,
    because there is no field.
    """

    policy_code: str
    version: str
    market: str
    scope: str
    sizing: tuple[PolicyDifference, ...]

    def as_details(self) -> dict[str, object]:
        return {
            "policy_code": self.policy_code,
            "version": self.version,
            "market": self.market,
            "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class CreateCommand:
    id: UUID
    policy_code: str
    operator: Operator
    source_ip: str
    correlation_id: str
    requested_at: datetime

    def __post_init__(self) -> None:
        if self.id.version != 7:
            raise ValueError("command id must be UUIDv7")
        object.__setattr__(self, "requested_at", require_utc(self.requested_at))


def new_create_command(
    *,
    policy_code: str,
    operator: Operator,
    source_ip: str,
    correlation_id: str,
    requested_at: datetime,
) -> CreateCommand:
    return CreateCommand(
        id=new_uuid7(),
        policy_code=policy_code,
        operator=operator,
        source_ip=source_ip,
        correlation_id=correlation_id,
        requested_at=requested_at,
    )


@dataclass(frozen=True, slots=True)
class PolicyCommand:
    id: UUID
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


def new_policy_command(
    *,
    target_version_id: UUID,
    operator: Operator,
    source_ip: str,
    correlation_id: str,
    approval_id: str,
    requested_at: datetime,
) -> PolicyCommand:
    return PolicyCommand(
        id=new_uuid7(),
        target_version_id=target_version_id,
        operator=operator,
        source_ip=source_ip,
        correlation_id=correlation_id,
        approval_id=approval_id,
        requested_at=requested_at,
    )


def approval_for(
    *, session_id: str, operator: Operator, facts: PolicyFacts
) -> ApprovalRequest:
    return ApprovalRequest(
        session_id=session_id,
        operator_email=operator.email,
        action=ACTIVATE,
        target_type=TARGET_TYPE,
        target_key=f"{facts.policy_code}:{facts.target_version}",
        authority_digest=facts.digest(),
    )


class MySqlPolicyCommands:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        approvals: ApprovalStore,
        ledger: MySqlCommandLedger | None = None,
    ) -> None:
        self._sessions = sessions
        self._reader = PoliciesReadModel(sessions)
        self._approvals = approvals
        self._ledger = ledger or MySqlCommandLedger(sessions)

    async def creatable(self) -> tuple[CreatableVersion, ...]:
        """Approved definitions the database does not hold yet.

        A deploy that adds a definition leaves the database a step behind. This
        is what the operator has to do about it, and until now the only way to
        do it was by hand.
        """
        stored = await self._stored_versions()
        return tuple(
            _creatable(definition)
            for definition in APPROVED_V6_RISK_POLICIES
            if (definition.code, definition.version) not in stored
        )

    async def create(self, command: CreateCommand) -> CreatableVersion:
        definition = approved_definition(command.policy_code)
        if definition is None:
            raise PolicyCommandRefusedError("that policy code is not approved")
        facts = _creatable(definition)
        await self._ledger.open(
            LedgerEntry(
                id=command.id,
                actor_email=command.operator.email,
                source_ip=command.source_ip,
                action=CREATE,
                target_type=TARGET_TYPE,
                target_key=f"{definition.code}:{definition.version}",
                payload={"correlation_id": command.correlation_id},
                expected_digest=authority_digest(facts.as_details()),
                started_at=command.requested_at,
            )
        )
        try:
            async with self._sessions() as session:
                await self._write(session, definition)
                await self._ledger.succeed(
                    session,
                    command_id=command.id,
                    result_code="CREATED",
                    result=facts.as_details(),
                    completed_at=command.requested_at,
                )
                await session.commit()
        except Exception as error:
            await self._ledger.fail(
                command_id=command.id,
                result_code=_failure_code(error),
                completed_at=command.requested_at,
            )
            raise
        return facts

    async def _write(
        self, session: AsyncSession, definition: V6RiskPolicyDefinition
    ) -> None:
        policy = await session.scalar(
            select(RiskPolicy)
            .where(RiskPolicy.code == definition.code)
            .with_for_update()
        )
        if policy is None:
            policy = RiskPolicy(id=new_uuid7(), code=definition.code, active=True)
            session.add(policy)
            await session.flush()
        existing = await session.scalar(
            select(RiskPolicyVersion).where(
                RiskPolicyVersion.policy_id == policy.id,
                RiskPolicyVersion.version == definition.version,
            )
        )
        if existing is not None:
            raise PolicyCommandRefusedError("that policy version is already stored")
        session.add(
            RiskPolicyVersion(
                id=new_uuid7(),
                policy_id=policy.id,
                version=definition.version,
                # Inert until activated. Creation writes the row; section 9
                # puts the second password on the step that makes it trade.
                active=False,
                **policy_row_values(definition),
            )
        )
        await session.flush()

    async def _stored_versions(self) -> frozenset[tuple[str, str]]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(RiskPolicy.code, RiskPolicyVersion.version).join(
                        RiskPolicyVersion,
                        RiskPolicyVersion.policy_id == RiskPolicy.id,
                    )
                )
            ).all()
        return frozenset((code, version) for code, version in rows)

    async def facts(self, target_version_id: UUID) -> PolicyFacts:
        versions = await self._reader.versions()
        target = _one(versions, target_version_id)
        if target is None:
            raise PolicyCommandRefusedError("that policy version is not stored")
        if target.active:
            raise PolicyCommandRefusedError("that policy version is already in force")
        await self._require_loadable(target_version_id, target.policy_code)
        active = next(
            (
                version
                for version in versions
                if version.active and version.policy_code == target.policy_code
            ),
            None,
        )
        return PolicyFacts(
            policy_code=target.policy_code,
            target_version=target.version,
            target_version_id=target.version_id,
            active_version=None if active is None else active.version,
            # Against nothing, every field is a change, which is exactly what
            # activating the first policy for a market is.
            differences=() if active is None else difference(active, target),
        )

    async def _require_loadable(self, version_id: UUID, code: str) -> None:
        """Refuse here rather than in the loop.

        Arming a version the loop cannot load does not fail at the button; it
        fails at the next evaluation, as a trader that will not start.
        """
        async with self._sessions() as session:
            row = await session.scalar(
                select(RiskPolicyVersion).where(RiskPolicyVersion.id == version_id)
            )
        if row is None:
            raise PolicyCommandRefusedError("that policy version is not stored")
        refusal = policy_row_refusal(row, code=code)
        if refusal is not None:
            raise PolicyCommandRefusedError(refusal)

    async def activate(self, command: PolicyCommand, *, session_id: str) -> PolicyFacts:
        facts = await self.facts(command.target_version_id)
        await self._ledger.open(
            LedgerEntry(
                id=command.id,
                actor_email=command.operator.email,
                source_ip=command.source_ip,
                action=ACTIVATE,
                target_type=TARGET_TYPE,
                target_key=f"{facts.policy_code}:{facts.target_version}",
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
                await self._switch(session, command.target_version_id)
                await self._ledger.succeed(
                    session,
                    command_id=command.id,
                    result_code="ACTIVATED",
                    result=facts.as_details(),
                    completed_at=command.requested_at,
                )
                # The version that takes effect and the record that it did
                # commit together.
                await session.commit()
        except Exception as error:
            await self._ledger.fail(
                command_id=command.id,
                result_code=_failure_code(error),
                completed_at=command.requested_at,
            )
            raise
        return facts

    async def _switch(self, session: AsyncSession, target_version_id: UUID) -> None:
        target = await session.scalar(
            select(RiskPolicyVersion)
            .where(RiskPolicyVersion.id == target_version_id)
            .with_for_update()
        )
        if target is None:
            raise PolicyCommandRefusedError("that policy version is not stored")
        others = (
            await session.scalars(
                select(RiskPolicyVersion)
                .where(
                    RiskPolicyVersion.policy_id == target.policy_id,
                    RiskPolicyVersion.active.is_(True),
                )
                .with_for_update()
            )
        ).all()
        for other in others:
            other.active = False
        # Both halves in one transaction. Between them the market would have
        # no policy, and the engine refuses to size without one.
        target.active = True
        await session.flush()


def _creatable(definition: V6RiskPolicyDefinition) -> CreatableVersion:
    return CreatableVersion(
        policy_code=definition.code,
        version=definition.version,
        market=definition.market.value,
        scope=definition.currency or definition.settlement_asset or "-",
        # Shown before the row exists, from the same mapping that writes it.
        sizing=tuple(
            PolicyDifference(name=name, left="-", right=_reading(value))
            for name, value in policy_row_values(definition).items()
        ),
    )


def _reading(value: object) -> str:
    if value is None:
        return "-"
    if type(value) is Decimal:
        return f"{value} ({as_percentage(value)})"
    return str(value)


def _one(
    versions: tuple[PolicyVersionView, ...], version_id: UUID
) -> PolicyVersionView | None:
    return next(
        (version for version in versions if version.version_id == version_id), None
    )


_FAILURE_CODES: dict[type[BaseException], str] = {
    PolicyCommandRefusedError: "POLICY_COMMAND_REFUSED",
}


def _failure_code(error: BaseException) -> str:
    """A stable code, because a message can be reworded and a grep cannot."""
    return _FAILURE_CODES.get(type(error), "UNEXPECTED_ERROR")


__all__ = (
    "ACTIVATE",
    "CREATE",
    "TARGET_TYPE",
    "CreatableVersion",
    "CreateCommand",
    "MySqlPolicyCommands",
    "PolicyCommand",
    "PolicyCommandRefusedError",
    "PolicyFacts",
    "approval_for",
    "new_create_command",
    "new_policy_command",
)
