from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.accounts import Account, AccountSnapshot


class AccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        broker_id: UUID,
        account_alias: str,
        environment: str,
        secret_reference: str,
        enabled: bool,
    ) -> Account:
        """`enabled` has no default on purpose.

        Section 9 puts the second password on account enablement. A create
        that quietly enabled would hand out the gated state without the gate,
        and a default here is exactly how that happens without anyone
        choosing it.
        """
        if len(re.sub(r"[^0-9]", "", account_alias)) >= 6:
            raise ValueError("plaintext account number is not allowed")
        if not secret_reference.startswith("secret://"):
            raise ValueError("secret_reference must use a secret manager reference")
        account = Account(
            broker_id=broker_id,
            account_alias=account_alias,
            environment=environment,
            secret_reference=secret_reference,
            enabled=enabled,
        )
        self._session.add(account)
        await self._session.flush()
        return account

    async def set_enabled(self, account_id: UUID, *, enabled: bool) -> Account:
        """Turn one account on or off, and say which it was."""
        account = await self._session.scalar(
            select(Account).where(Account.id == account_id).with_for_update()
        )
        if account is None:
            raise LookupError("that account is not stored")
        account.enabled = enabled
        await self._session.flush()
        return account


class AccountSnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_for_account(self, account_id: UUID) -> AccountSnapshot | None:
        return await self._session.scalar(
            select(AccountSnapshot)
            .where(AccountSnapshot.account_id == account_id)
            .order_by(AccountSnapshot.as_of.desc())
        )
