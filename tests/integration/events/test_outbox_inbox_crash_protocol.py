from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid7

import pytest
from alembic import command
from alembic.config import Config
from conftest import integration_database_url
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.contracts.envelope import EventEnvelope
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.events import (
    OpsInboxDeadLetter,
    OpsInboxEvent,
    OpsOutboxEvent,
)
from autotrader.persistence.mysql.models.operations import OpsIncident
from autotrader.persistence.mysql.outbox_dispatcher import OutboxDispatcher
from autotrader.persistence.mysql.repositories.inbox import InboxRepository, InboxResult
from autotrader.persistence.mysql.repositories.outbox import OutboxRepository

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 8, 9, tzinfo=UTC)


class Payload(BaseModel):
    value: str


def envelope(value: str) -> EventEnvelope[Payload]:
    return EventEnvelope(
        event_id=uuid7(),
        event_type="test.event.v1",
        schema_version=1,
        occurred_at=NOW,
        observed_at=NOW,
        producer="test-producer",
        partition_key="test",
        aggregate_type="test",
        aggregate_id=uuid7(),
        aggregate_version=1,
        correlation_id=uuid7(),
        causation_id=None,
        trace_id="test-trace",
        payload=Payload(value=value),
    )


@pytest.mark.integration
def test_outbox_claim_and_inbox_payload_conflict_are_durable() -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")
    command.upgrade(Config(ROOT / "alembic.ini"), "head")

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        original = envelope("one")
        conflict = original.model_copy(update={"payload": Payload(value="two")})
        try:
            async with session_factory() as session:
                outbox = OutboxRepository(session)
                await outbox.enqueue(original, next_attempt_at=NOW)
                await session.commit()

            async with session_factory() as session:
                claimed = await OutboxRepository(session).claim_batch(
                    limit=1,
                    now=NOW,
                    claim_owner="test-worker",
                    claim_expires_at=NOW + timedelta(minutes=1),
                )
                await session.commit()
                assert len(claimed) == 1
                assert claimed[0].event_id == original.event_id
                assert claimed[0].published_at is None

            async with session_factory() as session:
                inbox = InboxRepository(session)
                assert (
                    await inbox.begin_once("test-consumer", original) is InboxResult.NEW
                )
                await session.commit()

            async with session_factory() as session:
                inbox = InboxRepository(session)
                assert (
                    await inbox.begin_once("test-consumer", original)
                    is InboxResult.ALREADY_PROCESSED
                )
                await session.rollback()

            async with session_factory() as session:
                inbox = InboxRepository(session)
                assert (
                    await inbox.begin_once("test-consumer", conflict)
                    is InboxResult.PAYLOAD_CONFLICT
                )
                await session.rollback()

            async with session_factory() as session:
                await InboxRepository(session).record_payload_conflict(
                    consumer_name="test-consumer", envelope=conflict, occurred_at=NOW
                )
                await session.commit()
                assert await session.scalar(select(func.count(OpsOutboxEvent.id))) == 1
                assert (
                    await session.scalar(select(func.count(OpsInboxEvent.event_id)))
                    == 1
                )
                assert (
                    await session.scalar(select(func.count(OpsInboxDeadLetter.id))) == 1
                )
                assert await session.scalar(select(func.count(OpsIncident.id))) == 1
        finally:
            await engine.dispose()

    asyncio.run(verify())


@pytest.mark.integration
def test_inbox_concurrent_duplicate_is_not_a_transaction_error() -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")
    command.upgrade(Config(ROOT / "alembic.ini"), "head")

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        message = envelope("concurrent")

        async def begin() -> InboxResult:
            async with session_factory() as session:
                result = await InboxRepository(session).begin_once(
                    "concurrent", message
                )
                await session.commit()
                return result

        try:
            results = await asyncio.gather(begin(), begin())
            assert sorted(results) == [InboxResult.ALREADY_PROCESSED, InboxResult.NEW]
        finally:
            await engine.dispose()

    asyncio.run(verify())


@pytest.mark.integration
def test_outbox_dispatcher_acks_only_after_published_commit() -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")
    command.upgrade(Config(ROOT / "alembic.ini"), "head")

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        message = envelope("dispatch")
        published: list[object] = []
        acknowledged: list[object] = []

        async def publish(event: OpsOutboxEvent) -> None:
            published.append(event.event_id)

        async def acknowledge(event: OpsOutboxEvent) -> None:
            acknowledged.append(event.event_id)

        try:
            async with session_factory() as session:
                await OutboxRepository(session).enqueue(message, next_attempt_at=NOW)
                await session.commit()
            async with session_factory() as session:
                await OutboxRepository(session).claim_batch(
                    limit=1,
                    now=NOW,
                    claim_owner="dispatcher",
                    claim_expires_at=NOW + timedelta(minutes=1),
                )
                await session.commit()

            dispatcher = OutboxDispatcher(session_factory)
            await dispatcher.publish_then_acknowledge(
                event_id=message.event_id,
                claim_owner="dispatcher",
                now=NOW,
                published_at=NOW,
                publish=publish,
                acknowledge=acknowledge,
            )
            assert published == [message.event_id]
            assert acknowledged == [message.event_id]

            async def must_not_publish(event: OpsOutboxEvent) -> None:
                raise AssertionError(f"published duplicate {event.event_id}")

            await dispatcher.publish_then_acknowledge(
                event_id=message.event_id,
                claim_owner="dispatcher",
                now=NOW,
                published_at=NOW,
                publish=must_not_publish,
                acknowledge=acknowledge,
            )
            assert acknowledged == [message.event_id, message.event_id]
        finally:
            await engine.dispose()

    asyncio.run(verify())
