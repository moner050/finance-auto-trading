from __future__ import annotations

from collections.abc import Awaitable, Callable

from autotrader.persistence.redis.streams import RedisStreams, StreamEntry

StreamHandler = Callable[[str, dict[str, object]], Awaitable[None]]


class InboxConsumer:
    """ACKs a Redis entry only after its MySQL-backed handler has committed."""

    def __init__(
        self,
        *,
        streams: RedisStreams,
        stream: str,
        group: str,
        consumer: str,
        handler: StreamHandler,
    ) -> None:
        self._streams = streams
        self._stream = stream
        self._group = group
        self._consumer = consumer
        self._handler = handler
        self._claim_cursor = "0-0"

    async def consume_new(self, *, count: int) -> int:
        await self._streams.ensure_group(stream=self._stream, group=self._group)
        return await self._handle(
            await self._streams.read_group(
                stream=self._stream,
                group=self._group,
                consumer=self._consumer,
                count=count,
            )
        )

    async def recover_pending(self, *, min_idle_ms: int, count: int) -> int:
        await self._streams.ensure_group(stream=self._stream, group=self._group)
        claimed = await self._streams.claim_pending(
            stream=self._stream,
            group=self._group,
            consumer=self._consumer,
            min_idle_ms=min_idle_ms,
            count=count,
            start_id=self._claim_cursor,
        )
        self._claim_cursor = claimed.next_start_id
        return await self._handle(claimed.entries)

    async def _handle(self, entries: list[StreamEntry]) -> int:
        first_error: Exception | None = None
        handled = 0
        for entry in entries:
            try:
                await self._handler(entry.event_id, entry.body)
                await self._streams.acknowledge(
                    stream=self._stream, group=self._group, entry_id=entry.entry_id
                )
                handled += 1
            except Exception as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error
        return handled
