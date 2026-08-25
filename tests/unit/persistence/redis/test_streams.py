from __future__ import annotations

from collections.abc import Mapping

import pytest

from autotrader.persistence.redis.inbox_consumer import InboxConsumer
from autotrader.persistence.redis.streams import RedisStreams


class FakeRedis:
    def __init__(self) -> None:
        self.groups: set[tuple[str, str]] = set()
        self.new_entries: list[tuple[str, dict[str, str]]] = []
        self.pending: list[tuple[str, dict[str, str]]] = []
        self.acked: list[str] = []

    async def xgroup_create(
        self, name: str, groupname: str, *, id: str, mkstream: bool
    ) -> bool:
        key = (name, groupname)
        if key in self.groups:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        self.groups.add(key)
        return True

    async def xadd(self, name: str, fields: Mapping[str, str]) -> str:
        entry_id = f"{len(self.new_entries) + 1}-0"
        self.new_entries.append((entry_id, dict(fields)))
        return entry_id

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: Mapping[str, str],
        *,
        count: int,
        block: int,
    ) -> list[tuple[str, list[tuple[str, Mapping[str, str]]]]]:
        stream = next(iter(streams))
        entries = self.new_entries[:count]
        self.new_entries = self.new_entries[count:]
        self.pending.extend(entries)
        return [(stream, entries)] if entries else []

    async def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str,
        *,
        count: int,
    ) -> tuple[str, list[tuple[str, Mapping[str, str]]], list[str]]:
        candidates = self.pending
        if start_id != "0-0":
            candidates = [entry for entry in candidates if entry[0] >= start_id]
        entries = candidates[:count]
        remaining = candidates[count:]
        next_start_id = remaining[0][0] if remaining else "0-0"
        return next_start_id, entries, []

    async def xack(self, name: str, groupname: str, *ids: str) -> int:
        self.acked.extend(ids)
        self.pending = [entry for entry in self.pending if entry[0] not in ids]
        return len(ids)


@pytest.mark.asyncio
async def test_consumer_acknowledges_only_after_handler_commits() -> None:
    raw = FakeRedis()
    streams = RedisStreams(raw)
    await streams.publish(stream="stream:operations", event_id="event-1", body={"n": 1})
    committed: list[str] = []

    async def handler(event_id: str, body: dict[str, object]) -> None:
        committed.append(event_id)
        assert raw.acked == []

    consumer = InboxConsumer(
        streams=streams,
        stream="stream:operations",
        group="operations",
        consumer="worker-1",
        handler=handler,
    )

    assert await consumer.consume_new(count=1) == 1
    assert committed == ["event-1"]
    assert raw.acked == ["1-0"]


@pytest.mark.asyncio
async def test_failed_handler_remains_pending_for_bounded_recovery() -> None:
    raw = FakeRedis()
    streams = RedisStreams(raw)
    await streams.publish(stream="stream:operations", event_id="event-1", body={"n": 1})

    async def fail(_: str, __: dict[str, object]) -> None:
        raise RuntimeError("mysql commit failed")

    failed = InboxConsumer(
        streams=streams,
        stream="stream:operations",
        group="operations",
        consumer="dead-worker",
        handler=fail,
    )
    with pytest.raises(RuntimeError, match="mysql commit failed"):
        await failed.consume_new(count=1)
    assert raw.acked == []
    recovered: list[str] = []

    async def commit(event_id: str, _: dict[str, object]) -> None:
        recovered.append(event_id)

    survivor = InboxConsumer(
        streams=streams,
        stream="stream:operations",
        group="operations",
        consumer="survivor",
        handler=commit,
    )
    assert await survivor.recover_pending(min_idle_ms=1, count=1) == 1
    assert recovered == ["event-1"]
    assert raw.acked == ["1-0"]


@pytest.mark.asyncio
async def test_pending_failure_does_not_starve_later_entries() -> None:
    raw = FakeRedis()
    streams = RedisStreams(raw)
    await streams.publish(stream="stream:operations", event_id="poison", body={})
    await streams.publish(stream="stream:operations", event_id="healthy", body={})

    async def fail_once(_: str, __: dict[str, object]) -> None:
        raise RuntimeError("worker died")

    failed = InboxConsumer(
        streams=streams,
        stream="stream:operations",
        group="operations",
        consumer="dead-worker",
        handler=fail_once,
    )
    with pytest.raises(RuntimeError, match="worker died"):
        await failed.consume_new(count=2)
    processed: list[str] = []

    async def process_healthy(event_id: str, _: dict[str, object]) -> None:
        if event_id == "poison":
            raise RuntimeError("still poison")
        processed.append(event_id)

    recovered = InboxConsumer(
        streams=streams,
        stream="stream:operations",
        group="operations",
        consumer="survivor",
        handler=process_healthy,
    )
    with pytest.raises(RuntimeError, match="still poison"):
        await recovered.recover_pending(min_idle_ms=1, count=1)
    assert await recovered.recover_pending(min_idle_ms=1, count=1) == 1
    assert processed == ["healthy"]


@pytest.mark.asyncio
async def test_transport_preserves_inbox_payload_collision() -> None:
    raw = FakeRedis()
    streams = RedisStreams(raw)
    await streams.publish(
        stream="stream:risk", event_id="event-1", body={"value": "one"}
    )
    await streams.publish(
        stream="stream:risk", event_id="event-1", body={"value": "two"}
    )
    delivered: list[tuple[str, dict[str, object]]] = []

    async def handler(event_id: str, body: dict[str, object]) -> None:
        delivered.append((event_id, body))

    consumer = InboxConsumer(
        streams=streams,
        stream="stream:risk",
        group="risk",
        consumer="worker",
        handler=handler,
    )
    assert await consumer.consume_new(count=2) == 2
    assert delivered == [
        ("event-1", {"value": "one"}),
        ("event-1", {"value": "two"}),
    ]
