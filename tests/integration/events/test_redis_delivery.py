from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid7

import pytest
from pydantic import BaseModel
from redis import asyncio as redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.contracts.envelope import EventEnvelope
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.events import OpsInboxDeadLetter, OpsInboxEvent
from autotrader.persistence.mysql.models.operations import OpsIncident
from autotrader.persistence.redis.inbox_consumer import InboxConsumer
from autotrader.persistence.redis.mysql_inbox_handler import MySqlInboxHandler
from autotrader.persistence.redis.streams import RedisStreams

NOW = datetime(2026, 8, 9, tzinfo=UTC)


class Payload(BaseModel):
    value: str


def _redis_url() -> str:
    value = os.environ.get("REDIS_URL")
    if value is None:
        pytest.skip("REDIS_URL is required for Redis integration tests")
    return value


def _database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if value is None:
        pytest.skip("DATABASE_URL is required for MySQL/Redis integration tests")
    return value


def _envelope(value: str) -> EventEnvelope[Payload]:
    return EventEnvelope(
        event_id=uuid7(),
        event_type="test.redis.delivery.v1",
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


@pytest.mark.asyncio
@pytest.mark.integration
async def test_redis_ack_happens_after_handler_commit_and_pending_is_recovered() -> (
    None
):
    client = redis.from_url(_redis_url(), decode_responses=True)
    stream = f"test:redis-delivery:{uuid7()}"
    transport = RedisStreams(client)
    try:
        # Redis generates the entry id from the clock, so hold on to the one
        # publish returns rather than assuming a sequence.
        entry_id = await transport.publish(
            stream=stream, event_id="event-1", body={"value": "one"}
        )
        committed: list[str] = []

        async def crash_after_commit(event_id: str, _: dict[str, object]) -> None:
            committed.append(event_id)
            raise RuntimeError("simulated worker death after MySQL commit")

        failed = InboxConsumer(
            streams=transport,
            stream=stream,
            group="consumer-group",
            consumer="dead-worker",
            handler=crash_after_commit,
        )
        with pytest.raises(RuntimeError, match="simulated worker death"):
            await failed.consume_new(count=1)
        assert committed == ["event-1"]
        assert await client.xpending(stream, "consumer-group") == {
            "pending": 1,
            "min": entry_id,
            "max": entry_id,
            "consumers": [{"name": "dead-worker", "pending": 1}],
        }
        await asyncio.sleep(0.01)
        replayed: list[str] = []

        async def idempotent_commit(event_id: str, _: dict[str, object]) -> None:
            if event_id not in replayed:
                replayed.append(event_id)

        survivor = InboxConsumer(
            streams=transport,
            stream=stream,
            group="consumer-group",
            consumer="survivor",
            handler=idempotent_commit,
        )
        assert await survivor.recover_pending(min_idle_ms=1, count=1) == 1
        assert replayed == ["event-1"]
        assert (await client.xpending(stream, "consumer-group"))["pending"] == 0
    finally:
        await client.delete(stream)
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_redis_transport_preserves_conflicting_payload_for_mysql_inbox() -> None:
    client = redis.from_url(_redis_url(), decode_responses=True)
    stream = f"test:redis-conflict:{uuid7()}"
    transport = RedisStreams(client)
    received: list[tuple[str, dict[str, object]]] = []

    async def handler(event_id: str, body: dict[str, object]) -> None:
        received.append((event_id, body))

    consumer = InboxConsumer(
        streams=transport,
        stream=stream,
        group="consumer-group",
        consumer="worker",
        handler=handler,
    )
    try:
        await transport.publish(stream=stream, event_id="same-event", body={"v": "one"})
        await transport.publish(stream=stream, event_id="same-event", body={"v": "two"})
        assert await consumer.consume_new(count=2) == 2
        assert received == [
            ("same-event", {"v": "one"}),
            ("same-event", {"v": "two"}),
        ]
    finally:
        await client.delete(stream)
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_redis_redelivery_uses_mysql_inbox() -> None:
    database_url = _database_url()
    # The autouse fixture already migrated to head. Calling alembic from inside
    # an async test cannot work: its env.py drives the migration with
    # asyncio.run, which refuses to start inside a running loop.
    client = redis.from_url(_redis_url(), decode_responses=True)
    engine = create_engine(Settings(database_url=database_url))
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    stream = f"test:redis-inbox:{uuid7()}"
    transport = RedisStreams(client)
    original = _envelope("one")
    conflict = original.model_copy(update={"payload": Payload(value="two")})
    domain_effects: list[str] = []

    def parse_envelope(body: dict[str, object]) -> EventEnvelope[Payload]:
        return EventEnvelope[Payload].model_validate(body)

    async def apply_new(_: AsyncSession, envelope: EventEnvelope[Payload]) -> None:
        domain_effects.append(str(envelope.event_id))

    consumer = InboxConsumer(
        streams=transport,
        stream=stream,
        group="consumer-group",
        consumer="worker",
        handler=MySqlInboxHandler(
            session_factory=sessions,
            consumer_name="redis-consumer",
            parse_envelope=parse_envelope,
            apply_new=apply_new,
        ),
    )
    try:
        for envelope in (original, original, conflict):
            await transport.publish(
                stream=stream,
                event_id=str(envelope.event_id),
                body=envelope.model_dump(mode="json"),
            )
        assert await consumer.consume_new(count=3) == 3
        assert domain_effects == [str(original.event_id)]
        async with sessions() as session:
            assert await session.scalar(select(func.count(OpsInboxEvent.event_id))) == 1
            assert await session.scalar(select(func.count(OpsInboxDeadLetter.id))) == 1
            assert await session.scalar(select(func.count(OpsIncident.id))) == 1
    finally:
        await client.delete(stream)
        await client.aclose()
        await engine.dispose()
