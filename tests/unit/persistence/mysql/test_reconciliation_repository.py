from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.execution.reconciliation.models import ReconciliationRun
from autotrader.persistence.mysql.models.reconciliation import (
    PersistedReconciliationRun,
)
from autotrader.persistence.mysql.repositories.reconciliation import (
    ReconciliationRepository,
)

RUN_ID = UUID("019b0000-0000-7000-8000-000000000701")
RETRY_ID = UUID("019b0000-0000-7000-8000-000000000702")
BROKER_ID = UUID("019b0000-0000-7000-8000-000000000201")
ACCOUNT_ID = UUID("019b0000-0000-7000-8000-000000000211")
STARTED_AT = datetime(2026, 8, 19, 1, 2, 3, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 8, 19, 1, 2, 8, tzinfo=UTC)
SNAPSHOT_HASH = b"s" * 32


class _CursorResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _ReconciliationSession:
    def __init__(
        self,
        *,
        inserted: bool,
        persisted: PersistedReconciliationRun,
        diff_count: int = 0,
    ) -> None:
        self.inserted = inserted
        self.persisted = persisted
        self.diff_count = diff_count
        self.scalar_calls = 0
        self.added: list[object] = []
        self.flush_calls = 0

    async def execute(self, statement: object) -> _CursorResult:
        del statement
        return _CursorResult(1 if self.inserted else 0)

    async def scalar(self, statement: object) -> object:
        del statement
        self.scalar_calls += 1
        if self.scalar_calls == 1:
            return self.persisted
        if self.scalar_calls == 2:
            return self.diff_count
        raise AssertionError("unexpected scalar query")

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flush_calls += 1


def _run(*, run_id: UUID = RETRY_ID) -> ReconciliationRun:
    return ReconciliationRun(
        id=run_id,
        broker_id=BROKER_ID,
        account_id=ACCOUNT_ID,
        snapshot_hash=SNAPSHOT_HASH,
        complete=True,
        succeeded=True,
        diffs=(),
        started_at=STARTED_AT,
        completed_at=COMPLETED_AT,
    )


def _persisted_run(
    *,
    started_at: datetime = STARTED_AT,
    completed_at: datetime = COMPLETED_AT,
    status: str = "SUCCEEDED",
    complete: bool = True,
) -> PersistedReconciliationRun:
    return PersistedReconciliationRun(
        id=RUN_ID,
        broker_id=BROKER_ID,
        account_id=ACCOUNT_ID,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        snapshot_hash=SNAPSHOT_HASH,
        complete=complete,
    )


@pytest.mark.asyncio
async def test_exact_zero_diff_retry_returns_the_durable_run_identity() -> None:
    session = _ReconciliationSession(inserted=False, persisted=_persisted_run())

    stored = await ReconciliationRepository(cast(AsyncSession, session)).persist_run(
        _run()
    )

    assert stored == _run(run_id=RUN_ID)
    assert session.scalar_calls == 2
    assert session.added == []
    assert session.flush_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "persisted",
    (
        _persisted_run(started_at=STARTED_AT - timedelta(seconds=1)),
        _persisted_run(completed_at=COMPLETED_AT + timedelta(seconds=1)),
        _persisted_run(status="FAILED"),
        _persisted_run(complete=False),
    ),
    ids=("started-at", "completed-at", "status", "complete"),
)
async def test_zero_diff_retry_rejects_changed_immutable_run_fields(
    persisted: PersistedReconciliationRun,
) -> None:
    session = _ReconciliationSession(inserted=False, persisted=persisted)

    with pytest.raises(ValueError, match="reconciliation run evidence mismatch"):
        await ReconciliationRepository(cast(AsyncSession, session)).persist_run(_run())

    assert session.added == []
    assert session.flush_calls == 0


@pytest.mark.asyncio
async def test_zero_diff_retry_rejects_an_unexpected_persisted_diff() -> None:
    session = _ReconciliationSession(
        inserted=False,
        persisted=_persisted_run(),
        diff_count=1,
    )

    with pytest.raises(ValueError, match="reconciliation diff set mismatch"):
        await ReconciliationRepository(cast(AsyncSession, session)).persist_run(_run())

    assert session.scalar_calls == 2
    assert session.added == []
    assert session.flush_calls == 0


@pytest.mark.asyncio
async def test_new_zero_diff_run_returns_its_proposed_identity() -> None:
    proposed = _run(run_id=RUN_ID)
    session = _ReconciliationSession(inserted=True, persisted=_persisted_run())

    stored = await ReconciliationRepository(cast(AsyncSession, session)).persist_run(
        proposed
    )

    assert stored == proposed
    assert session.scalar_calls == 2
    assert session.added == []
    assert session.flush_calls == 1
