"""Which provider acts for an account, and which revision of that fact.

A binding is what the reconciliation runs attach to and what a LIVE activation
is granted against, so it is versioned rather than edited: a new revision is
written and activated, and the one it replaces stays where the runs that used
it can still point at it.

The scope is not a free field. The provider decides whether an account
sequence is required — Toss needs one, KIS and Binance must not have one — and
the environment comes from the account, because a binding whose environment
disagreed with its account's would name a combination the account does not
have. The schema refuses both; refusing them here means the operator is told
which, rather than being shown a constraint name.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.accounts import Account, Broker
from autotrader.persistence.mysql.models.bindings import ProviderAccountBinding
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc

# The providers the schema admits, and whether each names an account sequence.
SEQUENCED_PROVIDERS = frozenset({"TOSS"})
PROVIDERS = frozenset({"TOSS", "KIS", "BINANCE"})


def require_sequence_rule(provider_code: str, account_seq: int | None) -> None:
    """Toss identifies an account by a sequence; the other two do not have one.

    A number against KIS or Binance names nothing, and a missing one against
    Toss names no account at all. Exported so the back office can refuse the
    same thing before it asks for a second password, rather than after.
    """
    needs_sequence = provider_code in SEQUENCED_PROVIDERS
    if needs_sequence and (account_seq is None or account_seq <= 0):
        raise ProviderBindingRefusedError(
            "Toss 바인딩에는 양수 account_seq가 필요합니다."
        )
    if not needs_sequence and account_seq is not None:
        raise ProviderBindingRefusedError(
            f"{provider_code} 바인딩에는 account_seq를 둘 수 없습니다."
        )


class ProviderBindingRefusedError(RuntimeError):
    """Raised when a provider cannot be bound to an account as asked."""


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    """One revision of one provider acting for one account."""

    id: UUID
    account_id: UUID
    account_alias: str
    broker_id: UUID
    provider_code: str
    environment: str
    account_seq: int | None
    revision: int
    active: bool
    observed_at: datetime


class ProviderBindings:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_account(self, account_id: UUID) -> tuple[ProviderBinding, ...]:
        """Every revision, newest first, so the history is the screen."""
        rows = (
            await self._session.execute(
                select(ProviderAccountBinding, Account.account_alias)
                .join(Account, Account.id == ProviderAccountBinding.account_id)
                .where(ProviderAccountBinding.account_id == account_id)
                .order_by(
                    ProviderAccountBinding.provider_code,
                    ProviderAccountBinding.revision.desc(),
                )
            )
        ).all()
        return tuple(_view(row, alias) for row, alias in rows)

    async def active_for(
        self, account_id: UUID, *, provider_code: str
    ) -> ProviderBinding | None:
        found = (
            await self._session.execute(
                select(ProviderAccountBinding, Account.account_alias)
                .join(Account, Account.id == ProviderAccountBinding.account_id)
                .where(
                    ProviderAccountBinding.account_id == account_id,
                    ProviderAccountBinding.provider_code == provider_code,
                    ProviderAccountBinding.active.is_(True),
                )
            )
        ).first()
        return None if found is None else _view(*found)

    async def bind(
        self,
        *,
        account_id: UUID,
        provider_code: str,
        account_seq: int | None,
        now: datetime,
    ) -> ProviderBinding:
        """Write the next revision and make it the active one.

        Both halves are one flush. Between them the account would have a
        provider bound at no revision, and a reconciliation run starting in
        that gap would have nothing to attach to.
        """
        moment = require_utc(now)
        if provider_code not in PROVIDERS:
            raise ProviderBindingRefusedError("승인된 provider가 아닙니다.")
        # The same rule the confirmation panel applies. Kept here too: a
        # screen is not the only way into this repository.
        require_sequence_rule(provider_code, account_seq)

        account = await self._session.scalar(
            select(Account).where(Account.id == account_id).with_for_update()
        )
        if account is None:
            raise ProviderBindingRefusedError("저장되지 않은 계좌입니다.")
        broker_code = await self._session.scalar(
            select(Broker.code).where(Broker.id == account.broker_id)
        )
        if broker_code != provider_code:
            # The schema keys the binding to (broker_id, provider_code), so a
            # provider that is not this account's broker names a pair that
            # does not exist. Saying so beats a foreign key error.
            raise ProviderBindingRefusedError(
                f"이 계좌의 브로커는 {broker_code} 이며 {provider_code} 가 아닙니다."
            )

        current = await self._session.scalar(
            select(ProviderAccountBinding)
            .where(
                ProviderAccountBinding.account_id == account_id,
                ProviderAccountBinding.provider_code == provider_code,
                ProviderAccountBinding.active.is_(True),
            )
            .with_for_update()
        )
        if current is not None:
            current.active = False
        row = ProviderAccountBinding(
            id=new_uuid7(),
            account_id=account_id,
            broker_id=account.broker_id,
            provider_code=provider_code,
            # From the account, not from the caller. A binding that named an
            # environment its account does not have is a combination nothing
            # can act on.
            environment=account.environment,
            account_seq=account_seq,
            revision=await self._next_revision(account_id, provider_code),
            observed_at=moment,
            active=True,
        )
        self._session.add(row)
        await self._session.flush()
        return _view(row, account.account_alias)

    async def _next_revision(self, account_id: UUID, provider_code: str) -> int:
        highest = await self._session.scalar(
            select(func.max(ProviderAccountBinding.revision)).where(
                ProviderAccountBinding.account_id == account_id,
                ProviderAccountBinding.provider_code == provider_code,
            )
        )
        # Revisions start at one and never reuse a number, so a run that
        # recorded revision three still means the same thing next year.
        return 1 if highest is None else int(highest) + 1


def _view(row: ProviderAccountBinding, account_alias: str) -> ProviderBinding:
    return ProviderBinding(
        id=row.id,
        account_id=row.account_id,
        account_alias=account_alias,
        broker_id=row.broker_id,
        provider_code=row.provider_code,
        environment=row.environment,
        account_seq=row.account_seq,
        revision=row.revision,
        active=row.active,
        observed_at=require_utc(row.observed_at),
    )


__all__ = (
    "PROVIDERS",
    "SEQUENCED_PROVIDERS",
    "ProviderBinding",
    "ProviderBindingRefusedError",
    "ProviderBindings",
    "require_sequence_rule",
)
