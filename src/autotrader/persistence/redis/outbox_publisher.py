from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.events import OpsOutboxEvent
from autotrader.persistence.mysql.outbox_dispatcher import OutboxDispatcher
from autotrader.persistence.mysql.repositories.outbox import OutboxRepository
from autotrader.persistence.redis.streams import RedisStreams


class OutboxPublisher:
    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        streams: RedisStreams,
        stream: str,
        owner: str,
    ) -> None:
        self._sessions = session_factory
        self._streams = streams
        self._stream = stream
        self._owner = owner

    async def publish_available(self, *, now: datetime, limit: int) -> int:
        async with self._sessions() as session:
            claimed = await OutboxRepository(session).claim_batch(
                limit=limit,
                now=now,
                claim_owner=self._owner,
                claim_expires_at=now + timedelta(minutes=1),
            )
            await session.commit()
        dispatcher = OutboxDispatcher(self._sessions)
        for event in claimed:

            async def publish(value: OpsOutboxEvent) -> None:
                await self._streams.publish(
                    stream=self._stream,
                    event_id=str(value.event_id),
                    body=value.payload,
                )

            await dispatcher.publish_then_acknowledge(
                event_id=event.event_id,
                claim_owner=self._owner,
                now=now,
                published_at=now,
                publish=publish,
                acknowledge=lambda _: _noop(),
            )
        return len(claimed)


async def _noop() -> None:
    return None
