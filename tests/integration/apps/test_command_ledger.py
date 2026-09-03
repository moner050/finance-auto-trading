"""The command ledger against a real MySQL.

The schema's status column only means something if the transactions around it
are right, and no fake would tell you whether they are.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest
from conftest import integration_database_url
from sqlalchemy import select
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.ledger import (
    FAILED,
    IN_PROGRESS,
    SUCCEEDED,
    LedgerConflictError,
    LedgerEntry,
    MySqlCommandLedger,
    SourceAddressUnknownError,
    digest_of,
)
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.backoffice import BackofficeCommandRow

NOW = datetime(2026, 8, 27, tzinfo=UTC)


def _entry(**changes: object) -> LedgerEntry:
    values: dict[str, object] = {
        "id": uuid7(),
        "actor_email": "operator@example.com",
        "source_ip": "127.0.0.1",
        "action": "HALT",
        "target_type": "GLOBAL",
        "target_key": "ALL",
        "payload": {"action": "HALT"},
        "expected_digest": None,
        "started_at": NOW,
    }
    values.update(changes)
    return LedgerEntry(**values)  # type: ignore[arg-type]


def _drive(scenario: object) -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")

    async def run() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await scenario(sessions)  # type: ignore[operator]
        finally:
            await engine.dispose()

    asyncio.run(run())


async def _row(
    sessions: async_sessionmaker[AsyncSession],
) -> BackofficeCommandRow | None:
    async with sessions() as session:
        return await session.scalar(select(BackofficeCommandRow))


@pytest.mark.integration
def test_opening_a_command_is_durable_before_any_work_happens() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ledger = MySqlCommandLedger(sessions)

        await ledger.open(_entry())

        # Committed on its own. Nothing has been attempted yet, and the row is
        # already there to say something was going to be.
        row = await _row(sessions)
        assert row is not None
        assert row.status == IN_PROGRESS
        assert row.result_code is None
        assert row.completed_at is None

    _drive(scenario)


@pytest.mark.integration
def test_a_command_that_never_finishes_stays_visible_as_unfinished() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ledger = MySqlCommandLedger(sessions)
        entry = _entry()
        await ledger.open(entry)

        # The work dies here: nothing calls succeed or fail.
        row = await _row(sessions)

        assert row is not None
        # A crash mid-command reads as a crash rather than as nothing.
        assert row.status == IN_PROGRESS
        assert row.id == entry.id

    _drive(scenario)


@pytest.mark.integration
def test_a_success_that_cannot_be_recorded_takes_the_change_with_it() -> None:
    """The close runs in the caller's transaction, so rolling that back
    unmakes both the change and the record of it."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ledger = MySqlCommandLedger(sessions)
        entry = _entry()
        await ledger.open(entry)

        async with sessions() as session:
            await ledger.succeed(
                session,
                command_id=entry.id,
                result_code="APPLIED",
                result={"armed": False},
                completed_at=NOW,
            )
            await session.rollback()

        row = await _row(sessions)
        assert row is not None
        assert row.status == IN_PROGRESS

    _drive(scenario)


@pytest.mark.integration
def test_a_completed_command_records_its_outcome() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ledger = MySqlCommandLedger(sessions)
        entry = _entry()
        await ledger.open(entry)

        async with sessions() as session:
            await ledger.succeed(
                session,
                command_id=entry.id,
                result_code="APPLIED",
                result={"armed": False},
                completed_at=NOW + timedelta(seconds=1),
            )
            await session.commit()

        row = await _row(sessions)
        assert row is not None
        assert row.status == SUCCEEDED
        assert row.result_code == "APPLIED"
        assert row.result_digest == digest_of({"armed": False})
        assert row.completed_at is not None and row.completed_at >= row.started_at

    _drive(scenario)


@pytest.mark.integration
def test_a_failure_is_closed_on_its_own() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ledger = MySqlCommandLedger(sessions)
        entry = _entry()
        await ledger.open(entry)

        await ledger.fail(
            command_id=entry.id, result_code="NOTHING_TO_CONTROL", completed_at=NOW
        )

        row = await _row(sessions)
        assert row is not None
        assert row.status == FAILED
        assert row.result_code == "NOTHING_TO_CONTROL"
        # The schema wants a digest either way, and the code is the outcome.
        assert row.result_digest == digest_of({"failure": "NOTHING_TO_CONTROL"})

    _drive(scenario)


@pytest.mark.integration
def test_a_failure_never_overwrites_a_success() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ledger = MySqlCommandLedger(sessions)
        entry = _entry()
        await ledger.open(entry)
        async with sessions() as session:
            await ledger.succeed(
                session,
                command_id=entry.id,
                result_code="APPLIED",
                result={"armed": False},
                completed_at=NOW,
            )
            await session.commit()

        await ledger.fail(
            command_id=entry.id, result_code="LATE_FAILURE", completed_at=NOW
        )

        row = await _row(sessions)
        assert row is not None
        assert row.status == SUCCEEDED

    _drive(scenario)


@pytest.mark.integration
def test_the_same_command_cannot_be_opened_twice() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ledger = MySqlCommandLedger(sessions)
        entry = _entry()
        await ledger.open(entry)

        # The idempotency key is the primary key. A retried command is not a
        # second command.
        with pytest.raises(LedgerConflictError):
            await ledger.open(entry)

    _drive(scenario)


@pytest.mark.integration
def test_closing_a_command_nobody_opened_is_refused() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ledger = MySqlCommandLedger(sessions)

        with pytest.raises(LedgerConflictError):
            await ledger.fail(
                command_id=uuid7(), result_code="ANYTHING", completed_at=NOW
            )

    _drive(scenario)


@pytest.mark.integration
def test_the_database_refuses_a_command_from_anyone_else() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        # Not a check in application code. The schema itself says this
        # backoffice answers to one person, and MySQL reports a violated CHECK
        # as an operational error rather than an integrity one.
        with pytest.raises(DatabaseError, match="ck_backoffice_command_scope"):
            await MySqlCommandLedger(sessions).open(
                _entry(actor_email="someone@example.com")
            )

    _drive(scenario)


def test_a_command_with_no_source_address_is_refused() -> None:
    with pytest.raises(SourceAddressUnknownError):
        _entry(source_ip="   ")


def test_an_expected_digest_must_be_sha256() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        _entry(expected_digest=b"short")


def test_the_payload_digest_does_not_depend_on_key_order() -> None:
    assert _entry(payload={"a": 1, "b": 2}).payload_digest() == (
        _entry(payload={"b": 2, "a": 1}).payload_digest()
    )
