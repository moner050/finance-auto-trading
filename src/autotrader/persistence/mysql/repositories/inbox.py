from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TypeVar, cast

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.contracts.envelope import EventEnvelope
from autotrader.persistence.mysql.models.events import OpsInboxDeadLetter, OpsInboxEvent
from autotrader.persistence.mysql.models.operations import OpsIncident
from autotrader.persistence.mysql.repositories.operations import (
    lock_global_dispatch_guard,
)

PayloadT = TypeVar("PayloadT", bound=BaseModel)


class InboxResult(StrEnum):
    NEW = "NEW"
    ALREADY_PROCESSED = "ALREADY_PROCESSED"
    PAYLOAD_CONFLICT = "PAYLOAD_CONFLICT"


class InboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def begin_once(
        self, consumer_name: str, envelope: EventEnvelope[PayloadT]
    ) -> InboxResult:
        payload_hash = envelope.sha256()
        inserted = cast(
            CursorResult[object],
            await self._session.execute(
                insert(OpsInboxEvent)
                .values(
                    consumer_name=consumer_name,
                    event_id=envelope.event_id,
                    producer=envelope.producer,
                    aggregate_type=envelope.aggregate_type,
                    aggregate_id=envelope.aggregate_id,
                    aggregate_version=envelope.aggregate_version,
                    payload_hash=payload_hash,
                    status="PROCESSED",
                )
                .prefix_with("IGNORE")
            ),
        )
        existing = await self._session.scalar(
            select(OpsInboxEvent)
            .where(
                OpsInboxEvent.consumer_name == consumer_name,
                OpsInboxEvent.event_id == envelope.event_id,
            )
            .with_for_update()
        )
        if existing is None:
            raise RuntimeError("inbox insert did not persist an identity row")
        if inserted.rowcount == 1:
            return InboxResult.NEW
        if existing.payload_hash == payload_hash:
            return InboxResult.ALREADY_PROCESSED
        return InboxResult.PAYLOAD_CONFLICT

    async def record_payload_conflict(
        self,
        *,
        consumer_name: str,
        envelope: EventEnvelope[PayloadT],
        occurred_at: datetime,
    ) -> None:
        await lock_global_dispatch_guard(self._session)
        self._session.add(
            OpsInboxDeadLetter(
                consumer_name=consumer_name,
                event_id=envelope.event_id,
                payload_hash=envelope.sha256(),
                reason_code="INBOX_EVENT_ID_PAYLOAD_HASH_CONFLICT",
                created_at=occurred_at,
            )
        )
        self._session.add(
            OpsIncident(
                severity="BLOCKING",
                status="OPEN",
                reason_code="INBOX_EVENT_ID_PAYLOAD_HASH_CONFLICT",
                created_at=occurred_at,
            )
        )
        await self._session.flush()
