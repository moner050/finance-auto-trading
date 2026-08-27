from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from typing import cast
from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.execution.orders.models import WORKING_ORDER_STATUSES
from autotrader.execution.reconciliation.gates import ReconciliationRestartState
from autotrader.execution.reconciliation.models import (
    HeldPosition,
    InternalOpenOrder,
    ReconciliationDiff,
    ReconciliationRun,
)
from autotrader.persistence.mysql.models.accounts import Account
from autotrader.persistence.mysql.models.operations import OpsIncident
from autotrader.persistence.mysql.models.orders import (
    PersistedBrokerOrderLink,
    PersistedOrder,
)
from autotrader.persistence.mysql.models.positions import Position
from autotrader.persistence.mysql.models.reconciliation import (
    PersistedReconciliationDiff,
    PersistedReconciliationRun,
)
from autotrader.shared.ids import new_uuid7

# Derived from the enum rather than listed here, so a status added later
# cannot quietly fall outside reconciliation.
_WORKING_ORDER_STATUSES = tuple(
    sorted(status.value for status in WORKING_ORDER_STATUSES)
)


class ReconciliationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def restart_state(self, *, account_id: UUID) -> ReconciliationRestartState:
        latest = await self._session.scalar(
            select(PersistedReconciliationRun)
            .join(Account, Account.id == PersistedReconciliationRun.account_id)
            .where(PersistedReconciliationRun.account_id == account_id)
            .where(PersistedReconciliationRun.broker_id == Account.broker_id)
            .order_by(
                PersistedReconciliationRun.completed_at.desc(),
                PersistedReconciliationRun.id.desc(),
            )
            .limit(1)
        )
        blocking = await self._session.scalar(
            select(func.count(PersistedReconciliationDiff.id))
            .join(
                PersistedReconciliationRun,
                PersistedReconciliationDiff.run_id == PersistedReconciliationRun.id,
            )
            .join(Account, Account.id == PersistedReconciliationRun.account_id)
            .where(
                PersistedReconciliationRun.account_id == account_id,
                PersistedReconciliationRun.broker_id == Account.broker_id,
                PersistedReconciliationDiff.severity == "BLOCKING",
                PersistedReconciliationDiff.status == "OPEN",
            )
        )
        unknown = await self._session.scalar(
            select(func.count(PersistedOrder.id)).where(
                PersistedOrder.account_id == account_id,
                PersistedOrder.status == "UNKNOWN",
            )
        )
        return ReconciliationRestartState(
            latest_run_succeeded=(
                latest is not None
                and latest.status == "SUCCEEDED"
                and latest.completed_at is not None
            ),
            latest_run_complete=latest is not None and latest.complete,
            blocking_diff_count=int(blocking or 0),
            unknown_order_count=int(unknown or 0),
        )

    async def internal_positions(self, *, account_id: UUID) -> tuple[HeldPosition, ...]:
        """What this system believes the account holds.

        Flat instruments are left out rather than reported as zero, so that a
        broker reporting nothing and this system holding nothing agree instead
        of producing a difference in every instrument ever traded.
        """
        rows = await self._session.scalars(
            select(Position)
            .where(Position.account_id == account_id, Position.quantity != 0)
            .order_by(Position.instrument_id)
        )
        return tuple(
            HeldPosition(instrument_id=row.instrument_id, quantity=row.quantity)
            for row in rows
        )

    async def internal_open_orders(
        self, *, account_id: UUID
    ) -> tuple[InternalOpenOrder, ...]:
        """Orders this system believes are still working at the broker."""
        rows = (
            await self._session.execute(
                select(PersistedOrder, PersistedBrokerOrderLink)
                .join(
                    PersistedBrokerOrderLink,
                    PersistedBrokerOrderLink.order_id == PersistedOrder.id,
                )
                .where(
                    PersistedOrder.account_id == account_id,
                    PersistedOrder.status.in_(_WORKING_ORDER_STATUSES),
                )
                .order_by(PersistedBrokerOrderLink.broker_order_id)
            )
        ).all()
        return tuple(
            InternalOpenOrder(
                order_id=order.id,
                broker_order_id=link.broker_order_id,
                broker_client_order_id=order.broker_client_order_id,
            )
            for order, link in rows
        )

    async def enabled_restart_states(self) -> tuple[ReconciliationRestartState, ...]:
        account_ids = tuple(
            await self._session.scalars(
                select(Account.id).where(Account.enabled.is_(True)).order_by(Account.id)
            )
        )
        return tuple(
            [
                await self.restart_state(account_id=account_id)
                for account_id in account_ids
            ]
        )

    async def persist_run(self, run: ReconciliationRun) -> ReconciliationRun:
        inserted = (
            cast(
                CursorResult[object],
                await self._session.execute(
                    insert(PersistedReconciliationRun)
                    .values(
                        id=run.id,
                        broker_id=run.broker_id,
                        account_id=run.account_id,
                        started_at=run.started_at,
                        completed_at=run.completed_at,
                        status="SUCCEEDED" if run.succeeded else "FAILED",
                        snapshot_hash=run.snapshot_hash,
                        complete=run.complete,
                    )
                    .prefix_with("IGNORE")
                ),
            ).rowcount
            == 1
        )
        persisted_run = await self._session.scalar(
            select(PersistedReconciliationRun)
            .where(
                PersistedReconciliationRun.broker_id == run.broker_id,
                PersistedReconciliationRun.account_id == run.account_id,
                PersistedReconciliationRun.snapshot_hash == run.snapshot_hash,
            )
            .with_for_update()
        )
        if persisted_run is None:
            raise RuntimeError("inserted reconciliation run cannot be read")
        if (
            persisted_run.broker_id != run.broker_id
            or persisted_run.account_id != run.account_id
            or persisted_run.started_at != run.started_at
            or persisted_run.completed_at != run.completed_at
            or persisted_run.status != ("SUCCEEDED" if run.succeeded else "FAILED")
            or persisted_run.snapshot_hash != run.snapshot_hash
            or persisted_run.complete != run.complete
        ):
            raise ValueError("reconciliation run evidence mismatch")
        if not run.diffs:
            diff_count = await self._session.scalar(
                select(func.count(PersistedReconciliationDiff.id)).where(
                    PersistedReconciliationDiff.run_id == persisted_run.id
                )
            )
            if int(diff_count or 0) != 0:
                raise ValueError("reconciliation diff set mismatch")
        if not inserted:
            return replace(run, id=persisted_run.id)
        for diff in run.diffs:
            payload = {
                "broker_order_id": diff.broker_order_id,
                "instrument_id": str(diff.instrument_id)
                if diff.instrument_id
                else None,
                "internal_order_id": str(diff.internal_order_id)
                if diff.internal_order_id
                else None,
                "kind": diff.kind.value,
            }
            observed_hash = sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).digest()
            self._session.add(
                PersistedReconciliationDiff(
                    id=new_uuid7(),
                    run_id=persisted_run.id,
                    internal_order_id=diff.internal_order_id,
                    instrument_id=diff.instrument_id,
                    broker_order_id=diff.broker_order_id,
                    broker_execution_id=None,
                    diff_key=(
                        f"{diff.kind.value}:{diff.internal_order_id or ''}:"
                        f"{diff.broker_order_id or ''}:{diff.instrument_id or ''}"
                    ),
                    severity="BLOCKING" if diff.blocking else "INFO",
                    status="OPEN",
                    expected_hash=run.snapshot_hash,
                    observed_hash=observed_hash,
                    created_at=run.completed_at,
                    resolved_at=None,
                )
            )
            if diff.blocking:
                self._session.add(
                    OpsIncident(
                        severity="BLOCKING",
                        status="OPEN",
                        reason_code=f"RECONCILIATION_{diff.kind.value}",
                        scope_type=_scope_type(diff),
                        scope_key=str(
                            diff.internal_order_id
                            or diff.instrument_id
                            or persisted_run.id
                        ),
                        created_at=run.completed_at,
                    )
                )
        await self._session.flush()
        return replace(run, id=persisted_run.id)


def _scope_type(diff: ReconciliationDiff) -> str:
    """What an operator should go and look at."""
    if diff.internal_order_id is not None:
        return "ORDER"
    if diff.instrument_id is not None:
        return "INSTRUMENT"
    return "RECONCILIATION_RUN"
