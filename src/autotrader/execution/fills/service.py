from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from autotrader.execution.fills.models import BrokerExecutionEvent, Fill


@dataclass(frozen=True, slots=True)
class FillApplication:
    fill: Fill
    total_filled_quantity: Decimal
    overfill: bool


class FillStore(Protocol):
    async def apply_event_once(
        self, event: BrokerExecutionEvent
    ) -> FillApplication | None: ...


class FillService:
    def __init__(self, *, store: FillStore) -> None:
        self._store = store

    async def ingest(self, event: BrokerExecutionEvent) -> FillApplication | None:
        return await self._store.apply_event_once(event)
