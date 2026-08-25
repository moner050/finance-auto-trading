from __future__ import annotations

from typing import Protocol

from autotrader.execution.controls.models import GateDecision
from autotrader.execution.orders.models import BrokerOrderCommand


class BrokerDispatchAuthorizer(Protocol):
    """Authorizes durable commands; this port performs no broker I/O."""

    async def authorize(self, command: BrokerOrderCommand) -> GateDecision: ...
