from __future__ import annotations

from collections.abc import Mapping

import pytest

from autotrader.persistence.redis.inbox_consumer import InboxConsumer
from autotrader.persistence.redis.streams import RedisStreams


class FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, str]]] = []
        self.pending: list[tuple[str, dict[str, str]]] = []
        self.acked: list[str] = []
        self._group_created = False

    async def xgroup_create(
        self, name: str, groupname: str, *, id: str, mkstream: bool
    ) -> bool:
        if self._group_created:
            raise RuntimeError("BUSYGROUP")
        self._group_created = True
        return True

    async def xadd(self, name: str, fields: Mapping[str, str]) -> str:
        entry_id = f"{len(self.entries) + 1}-0"
        self.entries.append((entry_id, dict(fields)))
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
        entries = self.entries[:count]
        self.entries = self.entries[count:]
        self.pending.extend(entries)
        return [(next(iter(streams)), entries)] if entries else []

    async def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str,
        *,
        count: int,
    ) -> tuple[str, list[tuple[str, dict[str, str]]], list[str]]:
        return "0-0", self.pending[:count], []

    async def xack(self, name: str, groupname: str, *ids: str) -> int:
        self.acked.extend(ids)
        self.pending = [entry for entry in self.pending if entry[0] not in ids]
        return len(ids)


@pytest.mark.asyncio
@pytest.mark.chaos
async def test_redis_consumer_crash_replays_without_ack_before_commit() -> None:
    raw = FakeRedis()
    transport = RedisStreams(raw)
    await transport.publish(stream="stream:execution:fake", event_id="event-1", body={})
    domain_effects: set[str] = set()

    async def crash_after_commit(event_id: str, _: dict[str, object]) -> None:
        domain_effects.add(event_id)
        raise ConnectionError("worker process terminated")

    failed = InboxConsumer(
        streams=transport,
        stream="stream:execution:fake",
        group="execution",
        consumer="dead-worker",
        handler=crash_after_commit,
    )
    with pytest.raises(ConnectionError, match="terminated"):
        await failed.consume_new(count=1)
    assert raw.acked == []

    async def idempotent_handler(event_id: str, _: dict[str, object]) -> None:
        domain_effects.add(event_id)

    recovered = InboxConsumer(
        streams=transport,
        stream="stream:execution:fake",
        group="execution",
        consumer="survivor",
        handler=idempotent_handler,
    )
    assert await recovered.recover_pending(min_idle_ms=1, count=1) == 1
    assert domain_effects == {"event-1"}
    assert raw.acked == ["1-0"]
