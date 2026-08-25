from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.contracts.envelope import EventEnvelope
from autotrader.persistence.mysql.repositories.inbox import InboxRepository, InboxResult

PayloadT = TypeVar("PayloadT", bound=BaseModel)
EnvelopeParser = Callable[[dict[str, object]], EventEnvelope[PayloadT]]
DomainEffect = Callable[[AsyncSession, EventEnvelope[PayloadT]], Awaitable[None]]


class MySqlInboxHandler:
    """Commits inbox identity and the domain effect before Redis can ACK."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        consumer_name: str,
        parse_envelope: EnvelopeParser[PayloadT],
        apply_new: DomainEffect[PayloadT],
    ) -> None:
        self._sessions = session_factory
        self._consumer_name = consumer_name
        self._parse_envelope = parse_envelope
        self._apply_new = apply_new

    async def __call__(self, event_id: str, body: dict[str, object]) -> None:
        envelope = self._parse_envelope(body)
        if str(envelope.event_id) != event_id:
            raise ValueError("Redis event_id does not match envelope event_id")
        async with self._sessions() as session:
            inbox = InboxRepository(session)
            result = await inbox.begin_once(self._consumer_name, envelope)
            if result is InboxResult.NEW:
                await self._apply_new(session, envelope)
            elif result is InboxResult.PAYLOAD_CONFLICT:
                await inbox.record_payload_conflict(
                    consumer_name=self._consumer_name,
                    envelope=envelope,
                    occurred_at=envelope.observed_at,
                )
            await session.commit()
