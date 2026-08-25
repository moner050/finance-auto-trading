from __future__ import annotations

from typing import Protocol

from autotrader.execution.reconciliation.models import (
    BrokerOpenOrderAdoption,
    BrokerOpenOrderAdoptionResult,
)


class BrokerOpenOrderAdoptionStore(Protocol):
    async def adopt_open_order(
        self, adoption: BrokerOpenOrderAdoption
    ) -> BrokerOpenOrderAdoptionResult: ...


class BrokerOpenOrderAdoptionService:
    """Creates evidence for an already-open broker order; it never submits it."""

    def __init__(self, *, store: BrokerOpenOrderAdoptionStore) -> None:
        self._store = store

    async def adopt_open_order(
        self, adoption: BrokerOpenOrderAdoption
    ) -> BrokerOpenOrderAdoptionResult:
        return await self._store.adopt_open_order(adoption)
