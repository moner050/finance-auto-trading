from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from autotrader.execution.orders.models import BrokerOrderCommand
from autotrader.execution.reconciliation.models import BrokerOpenOrder, BrokerSnapshot


class FakeBrokerScenario(StrEnum):
    FULL_FILL = "FULL_FILL"
    PARTIAL_FILL = "PARTIAL_FILL"
    REJECT = "REJECT"
    TIMEOUT_BEFORE_ACCEPT = "TIMEOUT_BEFORE_ACCEPT"
    TIMEOUT_AFTER_ACCEPT = "TIMEOUT_AFTER_ACCEPT"
    ACCEPT_THEN_CRASH_BEFORE_RESULT_COMMIT = "ACCEPT_THEN_CRASH_BEFORE_RESULT_COMMIT"
    DUPLICATE_EVENTS = "DUPLICATE_EVENTS"
    REVERSED_STATUS_EVENTS = "REVERSED_STATUS_EVENTS"
    LATE_FILL_AFTER_CANCEL = "LATE_FILL_AFTER_CANCEL"
    PARTIAL_FILL_THEN_CANCEL = "PARTIAL_FILL_THEN_CANCEL"
    DUPLICATE_CANCEL = "DUPLICATE_CANCEL"
    REPLACE_LINEAGE = "REPLACE_LINEAGE"
    DUPLICATE_REPLACE = "DUPLICATE_REPLACE"
    TERMINAL_BEFORE_EXECUTION_CHECKPOINT = "TERMINAL_BEFORE_EXECUTION_CHECKPOINT"
    CHECKPOINT_BEFORE_TERMINAL = "CHECKPOINT_BEFORE_TERMINAL"
    OVERFILL = "OVERFILL"


class FakeBrokerTimeout(TimeoutError):
    """The caller cannot infer whether a broker accepted the command."""


class FakeBrokerProcessCrash(BaseException):
    """Simulates process death after the irreversible external acceptance."""


@dataclass(frozen=True, slots=True)
class FakeBrokerOrder:
    account_id: UUID
    broker_order_id: str
    broker_client_order_id: str
    scenario: FakeBrokerScenario
    canonical_terms_hash: bytes


@dataclass(frozen=True, slots=True)
class FakeBrokerEmission:
    """Opaque broker evidence plan; an application adapter publishes it as envelopes."""

    kind: str
    sequence: int


class FakeBroker:
    """A deterministic external boundary; it never calls application services."""

    def __init__(self, *, scenario: FakeBrokerScenario) -> None:
        self._scenario = scenario
        self._orders: dict[str, FakeBrokerOrder] = {}
        self._submit_count = 0
        self._cancel_count = 0
        self._replace_count = 0
        self._lookup_count = 0

    @property
    def submit_count(self) -> int:
        return self._submit_count

    @property
    def lookup_count(self) -> int:
        return self._lookup_count

    @property
    def cancel_count(self) -> int:
        return self._cancel_count

    @property
    def replace_count(self) -> int:
        return self._replace_count

    async def submit(self, command: BrokerOrderCommand) -> FakeBrokerOrder:
        if command.command_type.value != "SUBMIT":
            raise ValueError("FakeBroker only accepts SUBMIT commands in Task 15a")
        existing = self._orders.get(command.broker_client_order_id)
        if existing is not None:
            return existing
        if self._scenario is FakeBrokerScenario.TIMEOUT_BEFORE_ACCEPT:
            raise FakeBrokerTimeout(
                "fake broker timed out before accepting the command"
            )
        broker_order_id = uuid5(NAMESPACE_URL, command.broker_client_order_id)
        order = FakeBrokerOrder(
            account_id=command.account_id,
            broker_order_id=f"fake-{broker_order_id}",
            broker_client_order_id=command.broker_client_order_id,
            scenario=self._scenario,
            canonical_terms_hash=command.canonical_payload_hash,
        )
        self._orders[command.broker_client_order_id] = order
        self._submit_count += 1
        if self._scenario is FakeBrokerScenario.ACCEPT_THEN_CRASH_BEFORE_RESULT_COMMIT:
            raise FakeBrokerProcessCrash("fake broker accepted before process death")
        if self._scenario is FakeBrokerScenario.TIMEOUT_AFTER_ACCEPT:
            raise FakeBrokerTimeout("fake broker accepted the command before timeout")
        return order

    async def find_by_client_order_id(
        self, broker_client_order_id: str
    ) -> FakeBrokerOrder | None:
        self._lookup_count += 1
        return self._orders.get(broker_client_order_id)

    async def recover_submit(
        self, command: BrokerOrderCommand, *, now: datetime
    ) -> FakeBrokerOrder | None:
        del now
        return await self.find_by_client_order_id(command.broker_client_order_id)

    async def cancel(self, command: BrokerOrderCommand) -> FakeBrokerOrder:
        if command.command_type.value != "CANCEL":
            raise ValueError("FakeBroker cancel requires a CANCEL command")
        if command.target_broker_order_id is None:
            raise ValueError("cancel command requires a target broker order ID")
        order = next(
            (
                existing
                for existing in self._orders.values()
                if existing.broker_order_id == command.target_broker_order_id
            ),
            None,
        )
        if order is None:
            raise LookupError("fake broker order not found")
        self._cancel_count += 1
        return order

    async def replace(self, command: BrokerOrderCommand) -> FakeBrokerOrder:
        if command.command_type.value != "REPLACE":
            raise ValueError("FakeBroker replace requires a REPLACE command")
        if command.target_broker_order_id is None:
            raise ValueError("replace command requires a target broker order ID")
        predecessor = next(
            (
                existing
                for existing in self._orders.values()
                if existing.broker_order_id == command.target_broker_order_id
            ),
            None,
        )
        if predecessor is None:
            raise LookupError("fake broker order not found")
        existing = self._orders.get(command.broker_client_order_id)
        if existing is not None:
            return existing
        broker_order_id = uuid5(
            NAMESPACE_URL,
            f"{command.target_broker_order_id}:{command.broker_client_order_id}",
        )
        replacement = FakeBrokerOrder(
            account_id=command.account_id,
            broker_order_id=f"fake-{broker_order_id}",
            broker_client_order_id=command.broker_client_order_id,
            scenario=self._scenario,
            canonical_terms_hash=command.canonical_payload_hash,
        )
        self._orders[command.broker_client_order_id] = replacement
        self._replace_count += 1
        return replacement

    def emissions_for(
        self, broker_client_order_id: str
    ) -> tuple[FakeBrokerEmission, ...]:
        order = self._orders.get(broker_client_order_id)
        if order is None:
            return ()
        return _EMISSIONS[order.scenario]

    async def read_snapshot(
        self, *, broker_id: UUID, account_id: UUID, now: datetime
    ) -> BrokerSnapshot:
        if now.tzinfo is None:
            raise ValueError("snapshot time must be timezone-aware")
        return BrokerSnapshot(
            broker_id=broker_id,
            account_id=account_id,
            complete=True,
            expires_at=now.astimezone(UTC) + timedelta(minutes=1),
            open_orders=tuple(
                BrokerOpenOrder(
                    broker_order_id=order.broker_order_id,
                    broker_client_order_id=order.broker_client_order_id,
                    canonical_terms_hash=order.canonical_terms_hash,
                )
                for order in sorted(
                    (
                        order
                        for order in self._orders.values()
                        if order.account_id == account_id
                    ),
                    key=lambda order: order.broker_order_id,
                )
            ),
        )


_EMISSIONS: dict[FakeBrokerScenario, tuple[FakeBrokerEmission, ...]] = {
    FakeBrokerScenario.FULL_FILL: (
        FakeBrokerEmission("ACKNOWLEDGED", 1),
        FakeBrokerEmission("FILLED", 2),
    ),
    FakeBrokerScenario.PARTIAL_FILL: (
        FakeBrokerEmission("ACKNOWLEDGED", 1),
        FakeBrokerEmission("PARTIAL_FILL", 2),
    ),
    FakeBrokerScenario.REJECT: (FakeBrokerEmission("REJECTED", 1),),
    FakeBrokerScenario.TIMEOUT_BEFORE_ACCEPT: (),
    FakeBrokerScenario.TIMEOUT_AFTER_ACCEPT: (FakeBrokerEmission("ACKNOWLEDGED", 1),),
    FakeBrokerScenario.ACCEPT_THEN_CRASH_BEFORE_RESULT_COMMIT: (
        FakeBrokerEmission("ACKNOWLEDGED", 1),
    ),
    FakeBrokerScenario.DUPLICATE_EVENTS: (
        FakeBrokerEmission("ACKNOWLEDGED", 1),
        FakeBrokerEmission("ACKNOWLEDGED", 1),
    ),
    FakeBrokerScenario.REVERSED_STATUS_EVENTS: (
        FakeBrokerEmission("FILLED", 2),
        FakeBrokerEmission("ACKNOWLEDGED", 1),
    ),
    FakeBrokerScenario.LATE_FILL_AFTER_CANCEL: (
        FakeBrokerEmission("CANCELED", 1),
        FakeBrokerEmission("LATE_FILL", 2),
    ),
    FakeBrokerScenario.PARTIAL_FILL_THEN_CANCEL: (
        FakeBrokerEmission("PARTIAL_FILL", 1),
        FakeBrokerEmission("CANCELED", 2),
    ),
    FakeBrokerScenario.DUPLICATE_CANCEL: (
        FakeBrokerEmission("CANCELED", 1),
        FakeBrokerEmission("CANCELED", 1),
    ),
    FakeBrokerScenario.REPLACE_LINEAGE: (FakeBrokerEmission("REPLACE", 1),),
    FakeBrokerScenario.DUPLICATE_REPLACE: (
        FakeBrokerEmission("REPLACE", 1),
        FakeBrokerEmission("REPLACE", 1),
    ),
    FakeBrokerScenario.TERMINAL_BEFORE_EXECUTION_CHECKPOINT: (
        FakeBrokerEmission("CANCELED", 1),
        FakeBrokerEmission("CHECKPOINT", 2),
    ),
    FakeBrokerScenario.CHECKPOINT_BEFORE_TERMINAL: (
        FakeBrokerEmission("CHECKPOINT", 1),
        FakeBrokerEmission("CANCELED", 2),
    ),
    FakeBrokerScenario.OVERFILL: (
        FakeBrokerEmission("FILL", 1),
        FakeBrokerEmission("OVERFILL", 2),
    ),
}
