from __future__ import annotations

from datetime import datetime
from typing import TypeVar
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.contracts.envelope import EventEnvelope
from autotrader.persistence.mysql.models.events import OpsOutboxEvent

PayloadT = TypeVar("PayloadT", bound=BaseModel)


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self, envelope: EventEnvelope[PayloadT], *, next_attempt_at: datetime
    ) -> OpsOutboxEvent:
        event = OpsOutboxEvent(
            event_id=envelope.event_id,
            producer=envelope.producer,
            aggregate_type=envelope.aggregate_type,
            aggregate_id=envelope.aggregate_id,
            aggregate_version=envelope.aggregate_version,
            payload=envelope.model_dump(mode="json"),
            payload_hash=envelope.sha256(),
            next_attempt_at=next_attempt_at,
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def enqueue_once(
        self, envelope: EventEnvelope[PayloadT], *, next_attempt_at: datetime
    ) -> OpsOutboxEvent:
        payload_hash = envelope.sha256()
        await self._session.execute(
            insert(OpsOutboxEvent)
            .values(
                event_id=envelope.event_id,
                producer=envelope.producer,
                aggregate_type=envelope.aggregate_type,
                aggregate_id=envelope.aggregate_id,
                aggregate_version=envelope.aggregate_version,
                payload=envelope.model_dump(mode="json"),
                payload_hash=payload_hash,
                next_attempt_at=next_attempt_at,
            )
            .prefix_with("IGNORE")
        )
        existing = await self._session.scalar(
            select(OpsOutboxEvent)
            .where(OpsOutboxEvent.event_id == envelope.event_id)
            .with_for_update()
        )
        if existing is None:
            raise RuntimeError("inserted outbox event cannot be read")
        if existing.payload_hash != payload_hash:
            raise ValueError("outbox event identity payload collision")
        return existing

    async def claim_batch(
        self,
        *,
        limit: int,
        now: datetime,
        claim_owner: str,
        claim_expires_at: datetime,
    ) -> list[OpsOutboxEvent]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if claim_expires_at <= now:
            raise ValueError("claim_expires_at must be after now")
        events = list(
            (
                await self._session.scalars(
                    select(OpsOutboxEvent)
                    .where(
                        OpsOutboxEvent.published_at.is_(None),
                        OpsOutboxEvent.next_attempt_at <= now,
                        or_(
                            OpsOutboxEvent.claim_expires_at.is_(None),
                            OpsOutboxEvent.claim_expires_at <= now,
                        ),
                    )
                    .order_by(OpsOutboxEvent.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for event in events:
            event.claimed_by = claim_owner
            event.claim_expires_at = claim_expires_at
        await self._session.flush()
        return events

    async def mark_published(
        self,
        *,
        event_id: UUID,
        claim_owner: str,
        now: datetime,
        published_at: datetime,
    ) -> OpsOutboxEvent:
        event = await self._session.scalar(
            select(OpsOutboxEvent)
            .where(OpsOutboxEvent.event_id == event_id)
            .with_for_update()
        )
        if event is None:
            raise LookupError("outbox event not found")
        if event.claimed_by != claim_owner:
            raise PermissionError("outbox claim owner mismatch")
        if event.claim_expires_at is None or event.claim_expires_at <= now:
            raise PermissionError("outbox claim expired")
        if event.published_at is not None:
            return event
        event.published_at = published_at
        await self._session.flush()
        return event
