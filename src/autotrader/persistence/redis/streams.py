from __future__ import annotations

import json
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast


class RedisStreamClient(Protocol):
    """The subset of redis-py used by the reconstructible transport."""

    def xgroup_create(
        self, name: str, groupname: str, *, id: str, mkstream: bool
    ) -> Awaitable[bool]: ...

    def xadd(self, name: str, fields: Mapping[str, str]) -> Awaitable[str | bytes]: ...

    def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: Mapping[str, str],
        *,
        count: int,
        block: int,
    ) -> Awaitable[
        list[
            tuple[
                str | bytes,
                list[tuple[str | bytes, Mapping[str | bytes, str | bytes]]],
            ]
        ]
    ]: ...

    def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str,
        *,
        count: int,
    ) -> Awaitable[
        tuple[
            str | bytes,
            list[tuple[str | bytes, Mapping[str | bytes, str | bytes]]],
            list[str | bytes],
        ]
    ]: ...

    def xack(self, name: str, groupname: str, *ids: str) -> Awaitable[int]: ...


@dataclass(frozen=True)
class StreamEntry:
    entry_id: str
    event_id: str
    body: dict[str, object]


@dataclass(frozen=True)
class PendingClaim:
    next_start_id: str
    entries: list[StreamEntry]


class RedisStreams:
    """A bounded Redis Streams adapter; no authoritative state is held here."""

    def __init__(self, client: RedisStreamClient) -> None:
        self._client = client

    async def ensure_group(self, *, stream: str, group: str) -> None:
        try:
            await self._client.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def publish(
        self, *, stream: str, event_id: str, body: Mapping[str, object]
    ) -> str:
        entry_id = await self._client.xadd(
            stream,
            {"event_id": event_id, "body": json.dumps(body, sort_keys=True)},
        )
        return _text(entry_id)

    async def read_group(
        self, *, stream: str, group: str, consumer: str, count: int
    ) -> list[StreamEntry]:
        if count <= 0:
            raise ValueError("count must be positive")
        rows = await self._client.xreadgroup(
            group, consumer, {stream: ">"}, count=count, block=1
        )
        return [
            _entry(entry_id, fields)
            for _, entries in rows
            for entry_id, fields in entries
        ]

    async def claim_pending(
        self,
        *,
        stream: str,
        group: str,
        consumer: str,
        min_idle_ms: int,
        count: int,
        start_id: str,
    ) -> PendingClaim:
        if min_idle_ms <= 0:
            raise ValueError("min_idle_ms must be positive")
        if count <= 0:
            raise ValueError("count must be positive")
        next_start_id, entries, _ = await self._client.xautoclaim(
            stream, group, consumer, min_idle_ms, start_id, count=count
        )
        return PendingClaim(
            next_start_id=_text(next_start_id),
            entries=[_entry(entry_id, fields) for entry_id, fields in entries],
        )

    async def acknowledge(self, *, stream: str, group: str, entry_id: str) -> None:
        await self._client.xack(stream, group, entry_id)


def _entry(
    entry_id: str | bytes, fields: Mapping[str | bytes, str | bytes]
) -> StreamEntry:
    normalized = {_text(key): _text(value) for key, value in fields.items()}
    event_id = normalized.get("event_id")
    body_json = normalized.get("body")
    if not event_id or body_json is None:
        raise ValueError("Redis stream entry is missing event_id or body")
    decoded = json.loads(body_json)
    if not isinstance(decoded, dict):
        raise ValueError("Redis stream body must be a JSON object")
    decoded_object = cast(dict[object, object], decoded)
    if not all(isinstance(key, str) for key in decoded_object):
        raise ValueError("Redis stream body must be a JSON object")
    return StreamEntry(
        entry_id=_text(entry_id),
        event_id=event_id,
        body=cast(dict[str, object], decoded_object),
    )


def _text(value: str | bytes) -> str:
    return value.decode() if isinstance(value, bytes) else value
