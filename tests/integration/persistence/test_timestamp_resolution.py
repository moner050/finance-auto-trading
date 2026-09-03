"""Timestamps keep the order things happened in.

Twelve check constraints in this schema compare one timestamp against another
— `deactivated_at > activated_at`, `completed_at >= started_at`. A whole-second
column cannot support them: MySQL rounds what it cannot store, so two events a
tenth of a second apart either collapse together or swap places, and a rotation
performed in the right order gets refused for happening before the thing it
followed.

These pin the resolution rather than any one caller, because the next caller to
forget will be a different one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from conftest import integration_database_url
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.backoffice import BackofficeCommandRow
from autotrader.shared.ids import new_uuid7

STARTED = datetime(2026, 8, 27, 12, 0, 0, 700_000, tzinfo=UTC)
# A tenth of a second later. Rounded to seconds these swap: .7 becomes 13:00:01
# and .8 becomes 13:00:01 too, or worse, the earlier one rounds up past a
# later one that rounds down.
COMPLETED = STARTED + timedelta(milliseconds=100)


def _drive(scenario: object) -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("MySQL is required for integration tests")

    async def run() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await scenario(sessions)  # type: ignore[operator]
        finally:
            await engine.dispose()

    asyncio.run(run())


def _command(**changes: object) -> BackofficeCommandRow:
    values: dict[str, object] = {
        "id": new_uuid7(),
        "actor_email": "operator@example.com",
        "source_ip": "127.0.0.1",
        "action": "TEST",
        "target_type": "TEST",
        "target_key": "test",
        "payload_digest": b"p" * 32,
        "expected_digest": None,
        "status": "SUCCEEDED",
        "result_code": "OK",
        "result_digest": b"r" * 32,
        "started_at": STARTED,
        "completed_at": COMPLETED,
    }
    values.update(changes)
    return BackofficeCommandRow(**values)


@pytest.mark.integration
def test_a_sub_second_timestamp_survives_the_round_trip() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        row = _command()
        async with sessions() as session:
            session.add(row)
            await session.commit()
        async with sessions() as session:
            stored = await session.scalar(
                select(BackofficeCommandRow).where(BackofficeCommandRow.id == row.id)
            )
            assert stored is not None
            # Read before the rollback: it expires the instance, and a
            # detached one raises rather than returning what it last held.
            started, completed = stored.started_at, stored.completed_at
            await session.rollback()

        assert (started, completed) == (STARTED, COMPLETED)

    _drive(scenario)


@pytest.mark.integration
def test_two_events_a_tenth_of_a_second_apart_stay_in_order() -> None:
    """At whole-second resolution this pair is unorderable, and the constraint
    that reads it becomes a coin flip."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        row = _command()
        async with sessions() as session:
            session.add(row)
            # The `completed_at >= started_at` check has to accept this.
            await session.commit()
        async with sessions() as session:
            stored = await session.scalar(
                select(BackofficeCommandRow).where(BackofficeCommandRow.id == row.id)
            )
            assert stored is not None
            started, completed = stored.started_at, stored.completed_at
            await session.rollback()

        assert completed is not None
        assert completed > started

    _drive(scenario)
