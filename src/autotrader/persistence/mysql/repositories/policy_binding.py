"""Which risk policy version an account trades under.

Section 11.4 asks for one operation — activate or replace an account policy
binding — and one prohibition: the GUI does not broaden a market scope. Both
land here.

The scope is not a parameter. A binding's currency and settlement asset come
from the approved definition behind the policy version, so binding a KRW cash
policy to an account cannot quietly produce a USDT-denominated scope. There is
no argument a caller could pass to widen it, which is a stronger guarantee than
validating one it passed.

Resolution is fail-closed. A binding that points at a version the loop would
not load, or at one that is no longer in force, yields nothing rather than a
guess, because trading under a superseded policy is worse than not trading.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.risk import (
    AccountRiskPolicyBinding,
    RiskPolicy,
    RiskPolicyVersion,
)
from autotrader.persistence.mysql.repositories.david_v6_risk import (
    approved_definition,
    policy_row_refusal,
)
from autotrader.risk.models import V6RiskPolicySnapshot
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.models import V6Market

ACTIVE = "ACTIVE"
_SCOPE_DOMAIN = b"EXEC_ACCOUNT_POLICY_SCOPE_V1"


class BindingRefusedError(RuntimeError):
    """Raised when an account cannot be bound as asked."""


@dataclass(frozen=True, slots=True)
class PolicyBinding:
    """One account's policy, as it stands or as it stood."""

    id: UUID
    account_id: UUID
    policy_version_id: UUID
    policy_code: str
    version: str
    currency: str | None
    settlement_asset: str | None
    activated_at: datetime
    deactivated_at: datetime | None

    @property
    def scope(self) -> str:
        """What the account is denominated in, for a screen."""
        return self.currency or self.settlement_asset or "-"


def account_scope_hash(
    *, account_id: UUID, currency: str | None, settlement_asset: str | None
) -> bytes:
    """Binds the row to the scope it was written for.

    The columns can be edited; a digest over them cannot be edited into
    agreement without knowing it is there.
    """
    parts = (
        account_id.hex,
        currency or "",
        settlement_asset or "",
    )
    return hashlib.sha256(
        _SCOPE_DOMAIN + b"\x00" + b"\x00".join(part.encode("utf-8") for part in parts)
    ).digest()


class AccountPolicyBindings:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active_binding(self, account_id: UUID) -> PolicyBinding | None:
        row = await self._session.execute(
            select(AccountRiskPolicyBinding, RiskPolicy.code, RiskPolicyVersion.version)
            .join(
                RiskPolicyVersion,
                RiskPolicyVersion.id == AccountRiskPolicyBinding.policy_version_id,
            )
            .join(RiskPolicy, RiskPolicy.id == RiskPolicyVersion.policy_id)
            .where(
                AccountRiskPolicyBinding.account_id == account_id,
                AccountRiskPolicyBinding.active_marker == ACTIVE,
            )
        )
        found = row.first()
        return None if found is None else _view(*found)

    async def bind(
        self, *, account_id: UUID, policy_version_id: UUID, now: datetime
    ) -> PolicyBinding:
        """Replace the account's binding, or make its first one.

        Both halves are one flush, so there is no instant at which the account
        has no policy and the loop would refuse to size.
        """
        moment = require_utc(now)
        version, code = await self._version(policy_version_id)
        definition = approved_definition(code)
        if definition is None:
            raise BindingRefusedError("이 정책 코드는 승인된 v6 정책이 아닙니다.")
        refusal = policy_row_refusal(version, code=code)
        if refusal is not None:
            raise BindingRefusedError(refusal)
        if not version.active:
            # Binding to a version the loop will not load produces an account
            # that cannot trade, which is not what "bind" reads like.
            raise BindingRefusedError("적용 중인 버전만 계좌에 연결할 수 있습니다.")

        current = await self._locked_active(account_id)
        if current is not None and current.policy_version_id == policy_version_id:
            raise BindingRefusedError("이미 이 버전에 연결되어 있습니다.")
        if await self._bound_before(account_id, policy_version_id):
            # The schema keeps one row per (account, version), so a version an
            # account has already left cannot be returned to. Say that here
            # rather than surfacing a constraint name.
            raise BindingRefusedError(
                "이 계좌가 이전에 사용한 버전입니다. 새 버전을 만들어 연결하세요."
            )

        if current is not None:
            current.deactivated_at = moment
            current.active_marker = None
        row = AccountRiskPolicyBinding(
            id=new_uuid7(),
            account_id=account_id,
            policy_version_id=policy_version_id,
            previous_binding_id=None if current is None else current.id,
            currency=definition.currency,
            settlement_asset=definition.settlement_asset,
            account_scope_hash=account_scope_hash(
                account_id=account_id,
                currency=definition.currency,
                settlement_asset=definition.settlement_asset,
            ),
            activated_at=moment,
            deactivated_at=None,
            active_marker=ACTIVE,
        )
        self._session.add(row)
        await self._session.flush()
        return _view(row, code, version.version)

    async def resolve(
        self, account_id: UUID, *, market: V6Market
    ) -> V6RiskPolicySnapshot | None:
        """The policy this account trades under, or nothing.

        This is the read half. Without it the binding would be a record of a
        decision nothing acts on, and the loop would keep taking its policy
        from whoever wired it.
        """
        binding = await self.active_binding(account_id)
        if binding is None:
            return None
        definition = approved_definition(binding.policy_code)
        if definition is None or definition.market is not market:
            return None
        version, code = await self._version(binding.policy_version_id)
        if not version.active or policy_row_refusal(version, code=code) is not None:
            return None
        return definition.snapshot(binding.policy_version_id)

    async def _version(self, version_id: UUID) -> tuple[RiskPolicyVersion, str]:
        found = (
            await self._session.execute(
                select(RiskPolicyVersion, RiskPolicy.code)
                .join(RiskPolicy, RiskPolicy.id == RiskPolicyVersion.policy_id)
                .where(RiskPolicyVersion.id == version_id)
            )
        ).first()
        if found is None:
            raise BindingRefusedError("저장되지 않은 정책 버전입니다.")
        version, code = found
        return version, code

    async def _locked_active(self, account_id: UUID) -> AccountRiskPolicyBinding | None:
        return await self._session.scalar(
            select(AccountRiskPolicyBinding)
            .where(
                AccountRiskPolicyBinding.account_id == account_id,
                AccountRiskPolicyBinding.active_marker == ACTIVE,
            )
            .with_for_update()
        )

    async def _bound_before(self, account_id: UUID, version_id: UUID) -> bool:
        return (
            await self._session.scalar(
                select(AccountRiskPolicyBinding.id).where(
                    AccountRiskPolicyBinding.account_id == account_id,
                    AccountRiskPolicyBinding.policy_version_id == version_id,
                )
            )
        ) is not None


def _view(row: AccountRiskPolicyBinding, code: str, version: str) -> PolicyBinding:
    return PolicyBinding(
        id=row.id,
        account_id=row.account_id,
        policy_version_id=row.policy_version_id,
        policy_code=code,
        version=version,
        currency=row.currency,
        settlement_asset=row.settlement_asset,
        activated_at=require_utc(row.activated_at),
        deactivated_at=(
            None if row.deactivated_at is None else require_utc(row.deactivated_at)
        ),
    )


__all__ = (
    "ACTIVE",
    "AccountPolicyBindings",
    "BindingRefusedError",
    "PolicyBinding",
    "account_scope_hash",
)
