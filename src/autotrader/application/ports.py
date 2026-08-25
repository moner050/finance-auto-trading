from __future__ import annotations

from typing import Protocol, Self, TypeVar, runtime_checkable

from pydantic import BaseModel

from autotrader.contracts.envelope import EventEnvelope
from autotrader.strategies.common.decisions import StrategyDecision

PayloadT = TypeVar("PayloadT", bound=BaseModel)


@runtime_checkable
class AsyncUnitOfWork(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class OutboxRepositoryPort(Protocol):
    async def enqueue(
        self, envelope: EventEnvelope[PayloadT], *, next_attempt_at: object
    ) -> object: ...


class InboxRepositoryPort(Protocol):
    async def begin_once(
        self, consumer_name: str, envelope: EventEnvelope[PayloadT]
    ) -> object: ...


class StrategyRepositoryPort(Protocol):
    async def persist_decision(self, decision: StrategyDecision) -> object: ...


class AccountSnapshotRepositoryPort(Protocol):
    async def latest_for_account(self, account_id: object) -> object: ...


class PositionReaderPort(Protocol):
    async def get(self, position_id: object) -> object: ...


class OrderIntentRepositoryPort(Protocol):
    async def create_or_get(self, intent: object) -> object: ...


class RiskBudgetAnchorRepositoryPort(Protocol):
    async def lock_global_and_account(
        self, *, account_id: object, currency: str
    ) -> object: ...


class RiskDecisionRepositoryPort(Protocol):
    async def persist(self, decision: object) -> object: ...


class RiskReservationRepositoryPort(Protocol):
    async def persist(self, reservation: object) -> object: ...


class OrderRepositoryPort(Protocol):
    async def create_approved_once(
        self,
        *,
        order: object,
        command: object,
        event: object,
        envelope: EventEnvelope[PayloadT],
    ) -> object: ...
