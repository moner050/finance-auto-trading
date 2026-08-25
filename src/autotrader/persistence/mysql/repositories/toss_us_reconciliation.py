from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.integrations.brokers.toss.submit_recovery import (
    TossRecoveryClaim,
    TossRecoveryRecord,
    TossRecoveryState,
)
from autotrader.persistence.mysql.models.toss_us_reconciliation import (
    TossUsCashFactRow,
    TossUsOrderFactRow,
    TossUsPositionFactRow,
    TossUsReconciliationRunRow,
    TossUsRecoveryLeaseRow,
)


class TossUsRecoveryLeaseRepository:
    """Owns short transactions around the exclusive Toss replay lease."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def prepare(self, record: TossRecoveryRecord) -> TossRecoveryClaim:
        if type(record) is not TossRecoveryRecord:
            raise TypeError("record must be an exact TossRecoveryRecord")
        record.validate()
        try:
            return await self._prepare_transaction(record)
        except IntegrityError:
            return await self._read_after_insert_race(record)

    async def load(self, dispatch_id: UUID) -> TossRecoveryRecord | None:
        async with self._sessions() as session:
            row = await session.get(TossUsRecoveryLeaseRow, dispatch_id)
            return None if row is None else _recovery_record(row)

    async def claim_replay(
        self,
        dispatch_id: UUID,
        *,
        lease_owner: UUID,
        now: datetime,
        request_digest: bytes,
    ) -> TossRecoveryClaim:
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(TossUsRecoveryLeaseRow)
                .where(TossUsRecoveryLeaseRow.id == dispatch_id)
                .with_for_update()
            )
            if row is None:
                raise LookupError("Toss US recovery lease does not exist")
            current = _recovery_record(row)
            if current.request_digest != request_digest:
                raise ValueError("Toss recovery request digest mismatch")
            if current.state is not TossRecoveryState.OPEN:
                return TossRecoveryClaim(current, acquired=False)
            if now >= current.lease_expires_at:
                row.terminal_state = TossRecoveryState.UNKNOWN.value
                row.terminal_at = now
                row.active_marker = None
                await session.flush()
                return TossRecoveryClaim(_recovery_record(row), acquired=False)
            if current.replay_count >= 1:
                return TossRecoveryClaim(current, acquired=False)
            row.lease_owner = lease_owner
            row.lease_acquired_at = now
            row.replay_count = 1
            await session.flush()
            return TossRecoveryClaim(_recovery_record(row), acquired=True)

    async def finish(
        self,
        dispatch_id: UUID,
        *,
        lease_owner: UUID,
        state: TossRecoveryState,
        terminal_at: datetime,
        provider_order_id: str | None,
    ) -> TossRecoveryRecord:
        if state is TossRecoveryState.OPEN:
            raise ValueError("OPEN is not a Toss recovery outcome")
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(TossUsRecoveryLeaseRow)
                .where(TossUsRecoveryLeaseRow.id == dispatch_id)
                .with_for_update()
            )
            if row is None:
                raise LookupError("Toss US recovery lease does not exist")
            if (
                row.terminal_state != TossRecoveryState.OPEN.value
                or row.lease_owner != lease_owner
            ):
                raise RuntimeError("stale Toss recovery lease owner")
            candidate = TossRecoveryRecord(
                dispatch_id=row.id,
                binding_id=row.binding_id,
                account_id=row.account_id,
                client_order_id=row.client_order_id,
                first_dispatch_at=row.first_dispatch_at,
                request_digest=row.canonical_request_digest,
                lease_owner=row.lease_owner,
                lease_acquired_at=row.lease_acquired_at,
                lease_expires_at=row.lease_expires_at,
                replay_count=row.replay_count,
                state=state,
                terminal_at=terminal_at,
                provider_order_id=provider_order_id,
            )
            candidate.validate()
            row.terminal_state = state.value
            row.terminal_at = terminal_at
            row.provider_order_id = provider_order_id
            row.active_marker = None
            await session.flush()
            return candidate

    async def _prepare_transaction(
        self, record: TossRecoveryRecord
    ) -> TossRecoveryClaim:
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(TossUsRecoveryLeaseRow)
                .where(TossUsRecoveryLeaseRow.id == record.dispatch_id)
                .with_for_update()
            )
            if row is not None:
                return _existing_recovery_claim(row, record)
            session.add(
                TossUsRecoveryLeaseRow(
                    id=record.dispatch_id,
                    binding_id=record.binding_id,
                    account_id=record.account_id,
                    client_order_id=record.client_order_id,
                    first_dispatch_at=record.first_dispatch_at,
                    canonical_request_digest=record.request_digest,
                    lease_owner=record.lease_owner,
                    lease_acquired_at=record.lease_acquired_at,
                    lease_expires_at=record.lease_expires_at,
                    replay_count=record.replay_count,
                    terminal_state=record.state.value,
                    terminal_at=None,
                    provider_order_id=None,
                    active_marker="ACTIVE",
                )
            )
            await session.flush()
            return TossRecoveryClaim(record, acquired=True)

    async def _read_after_insert_race(
        self, record: TossRecoveryRecord
    ) -> TossRecoveryClaim:
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(TossUsRecoveryLeaseRow)
                .where(TossUsRecoveryLeaseRow.id == record.dispatch_id)
                .with_for_update()
            )
            if row is None:
                raise RuntimeError("another Toss US recovery already owns this binding")
            return _existing_recovery_claim(row, record)


class TossUsReconciliationRepository:
    """Appends one complete reconciliation bundle in the caller transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_checkpoint(
        self,
        *,
        binding_id: UUID,
        provider_as_of: datetime,
    ) -> TossUsReconciliationRunRow | None:
        return await self._session.scalar(
            select(TossUsReconciliationRunRow).where(
                TossUsReconciliationRunRow.binding_id == binding_id,
                TossUsReconciliationRunRow.provider_as_of == provider_as_of,
                TossUsReconciliationRunRow.result == "IN_PROGRESS",
            )
        )

    async def persist_checkpoint(
        self,
        run: TossUsReconciliationRunRow,
    ) -> TossUsReconciliationRunRow:
        _validate_checkpoint(run)
        existing = await self._session.scalar(
            select(TossUsReconciliationRunRow)
            .where(
                TossUsReconciliationRunRow.binding_id == run.binding_id,
                TossUsReconciliationRunRow.provider_as_of == run.provider_as_of,
            )
            .with_for_update()
        )
        if existing is None:
            self._session.add(run)
            stored = run
        else:
            if (
                existing.result != "IN_PROGRESS"
                or existing.id != run.id
                or existing.account_id != run.account_id
                or existing.started_at != run.started_at
                or existing.updated_at > run.updated_at
            ):
                raise ValueError("Toss US completed reconciliation is append-only")
            existing.updated_at = run.updated_at
            existing.checkpoint = run.checkpoint
            stored = existing
        await self._session.flush()
        return stored

    async def persist_completed_run(
        self,
        *,
        run: TossUsReconciliationRunRow,
        cash_facts: Sequence[TossUsCashFactRow],
        position_facts: Sequence[TossUsPositionFactRow],
        order_facts: Sequence[TossUsOrderFactRow],
    ) -> TossUsReconciliationRunRow:
        if type(run) is not TossUsReconciliationRunRow:
            raise TypeError("run must be an exact TossUsReconciliationRunRow")
        cash = _exact_rows(cash_facts, TossUsCashFactRow, "cash_facts")
        positions = _exact_rows(
            position_facts,
            TossUsPositionFactRow,
            "position_facts",
        )
        orders = _exact_rows(order_facts, TossUsOrderFactRow, "order_facts")
        _validate_bundle(run, cash, positions, orders)

        existing = await self._session.scalar(
            select(TossUsReconciliationRunRow)
            .where(TossUsReconciliationRunRow.id == run.id)
            .with_for_update()
        )
        if existing is None:
            self._session.add(run)
        else:
            _finalize_checkpoint(existing, run)
        self._session.add_all([*cash, *positions, *orders])
        await self._session.flush()
        return run

    async def update_completed_run(self, run: TossUsReconciliationRunRow) -> None:
        del run
        raise ValueError("Toss US completed reconciliation is append-only")

    async def delete_completed_run(self, run_id: UUID) -> None:
        del run_id
        raise ValueError("Toss US completed reconciliation is append-only")


def _exact_rows[RowT](
    rows: Sequence[RowT],
    row_type: type[RowT],
    name: str,
) -> tuple[RowT, ...]:
    if isinstance(rows, (str, bytes)):
        raise TypeError(f"{name} must be a row sequence")
    result = tuple(rows)
    if any(type(row) is not row_type for row in result):
        raise TypeError(f"{name} contains an invalid row")
    return result


def _validate_bundle(
    run: TossUsReconciliationRunRow,
    cash: tuple[TossUsCashFactRow, ...],
    positions: tuple[TossUsPositionFactRow, ...],
    orders: tuple[TossUsOrderFactRow, ...],
) -> None:
    if run.provider_code != "TOSS" or run.market_country != "US":
        raise ValueError("reconciliation run is outside Toss US scope")
    if run.settlement_asset != "USD":
        raise ValueError("Toss US reconciliation must use USD")
    if type(run.fact_digest) is not bytes or len(run.fact_digest) != 32:
        raise ValueError("fact_digest must be SHA-256 bytes")
    if run.completed_at is None or run.checkpoint is not None:
        raise ValueError("completed reconciliation shape is invalid")
    if (
        run.cash_fact_count != len(cash)
        or run.position_fact_count != len(positions)
        or run.order_fact_count != len(orders)
    ):
        raise ValueError("reconciliation fact counts do not match the bundle")
    for row in (*cash, *positions, *orders):
        if row.run_id != run.id:
            raise ValueError("reconciliation fact has the wrong run identity")

    page_missing = (
        run.missing_page_count > 0
        or run.holdings_page_count < 1
        or run.open_order_page_count < 1
        or run.closed_order_page_count < 1
    )
    blocked = bool(run.blockers)
    passing = not page_missing and not blocked and len(cash) == 1
    expected = "COMPLETE" if passing else "PARTIAL"
    if run.result != expected:
        raise ValueError("missing page or blocker makes reconciliation non-passing")


def _validate_checkpoint(run: TossUsReconciliationRunRow) -> None:
    if type(run) is not TossUsReconciliationRunRow:
        raise TypeError("run must be an exact TossUsReconciliationRunRow")
    if (
        run.result != "IN_PROGRESS"
        or run.completed_at is not None
        or run.fact_digest is not None
        or type(run.checkpoint) is not dict
        or run.blockers
        or run.provider_code != "TOSS"
        or run.market_country != "US"
        or run.settlement_asset != "USD"
    ):
        raise ValueError("Toss US checkpoint shape is invalid")


def _finalize_checkpoint(
    existing: TossUsReconciliationRunRow,
    completed: TossUsReconciliationRunRow,
) -> None:
    if (
        existing.result != "IN_PROGRESS"
        or existing.binding_id != completed.binding_id
        or existing.account_id != completed.account_id
        or existing.provider_as_of != completed.provider_as_of
        or existing.started_at != completed.started_at
    ):
        raise ValueError("Toss US completed reconciliation is append-only")
    for name in (
        "updated_at",
        "completed_at",
        "result",
        "holdings_page_count",
        "open_order_page_count",
        "closed_order_page_count",
        "missing_page_count",
        "cash_fact_count",
        "position_fact_count",
        "order_fact_count",
        "fact_digest",
        "blockers",
        "checkpoint",
    ):
        setattr(existing, name, getattr(completed, name))


def _existing_recovery_claim(
    row: TossUsRecoveryLeaseRow,
    expected: TossRecoveryRecord,
) -> TossRecoveryClaim:
    current = _recovery_record(row)
    if (
        current.binding_id != expected.binding_id
        or current.account_id != expected.account_id
        or current.client_order_id != expected.client_order_id
        or current.first_dispatch_at != expected.first_dispatch_at
        or current.request_digest != expected.request_digest
    ):
        raise ValueError("Toss recovery evidence mismatch")
    return TossRecoveryClaim(current, acquired=False)


def _recovery_record(row: TossUsRecoveryLeaseRow) -> TossRecoveryRecord:
    record = TossRecoveryRecord(
        dispatch_id=row.id,
        binding_id=row.binding_id,
        account_id=row.account_id,
        client_order_id=row.client_order_id,
        first_dispatch_at=row.first_dispatch_at,
        request_digest=row.canonical_request_digest,
        lease_owner=row.lease_owner,
        lease_acquired_at=row.lease_acquired_at,
        lease_expires_at=row.lease_expires_at,
        replay_count=row.replay_count,
        state=TossRecoveryState(row.terminal_state),
        terminal_at=row.terminal_at,
        provider_order_id=row.provider_order_id,
    )
    record.validate()
    return record
