from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.events import OpsOutboxEvent
from autotrader.persistence.mysql.repositories.outbox import OutboxRepository


class OutboxDispatcher:
    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def publish_then_acknowledge(
        self,
        *,
        event_id: UUID,
        claim_owner: str,
        now: datetime,
        published_at: datetime,
        publish: Callable[[OpsOutboxEvent], Awaitable[None]],
        acknowledge: Callable[[OpsOutboxEvent], Awaitable[None]],
    ) -> None:
        async with self._session_factory() as session:
            event = await session.scalar(
                select(OpsOutboxEvent)
                .where(OpsOutboxEvent.event_id == event_id)
                .with_for_update()
            )
            if event is None:
                raise LookupError("outbox event not found")
            if event.claimed_by != claim_owner:
                raise PermissionError("outbox claim owner mismatch")
            if event.published_at is not None:
                already_published = event
            else:
                if event.claim_expires_at is None or event.claim_expires_at <= now:
                    raise PermissionError("outbox claim expired")
                already_published = None

        if already_published is not None:
            await acknowledge(already_published)
            return
        await publish(event)

        async with self._session_factory() as session:
            published = await OutboxRepository(session).mark_published(
                event_id=event_id,
                claim_owner=claim_owner,
                now=now,
                published_at=published_at,
            )
            await session.commit()
        await acknowledge(published)
