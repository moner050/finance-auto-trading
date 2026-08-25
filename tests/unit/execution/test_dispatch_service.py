from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from inspect import getdoc, signature
from pathlib import Path
from uuid import UUID, uuid7

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.dispatch.service import DispatchService, DispatchStore
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.common import BrokerSubmissionRejected
from autotrader.integrations.brokers.fake.adapter import (
    FakeBroker,
    FakeBrokerScenario,
)


@dataclass
class MemoryDispatchStore:
    command: BrokerOrderCommand
    durable_status: str = "PENDING"
    attempt_recorded: bool = False
    unknown_recorded: bool = False
    rejected_recorded: bool = False
    accepted_broker_order_id: str | None = None
    recovery_attempts: int = 0
    unknown_attempts: int = 0

    async def authorize_and_record_attempt(
        self, *, command_id: UUID, now: datetime
    ) -> BrokerOrderCommand | None:
        assert command_id == self.command.id
        assert now < self.command.not_after
        if self.attempt_recorded:
            return None
        self.attempt_recorded = True
        self.durable_status = "DISPATCHING"
        self.command = replace(self.command, dispatch_attempted_at=now)
        return self.command

    async def record_unknown(
        self, *, command_id: UUID, now: datetime, deadline: datetime
    ) -> None:
        assert command_id == self.command.id
        assert deadline == self.command.not_after
        self.unknown_attempts += 1
        if self.durable_status == "ACCEPTED":
            return
        self.unknown_recorded = True
        self.durable_status = "UNKNOWN"

    async def record_accepted(
        self, *, command_id: UUID, broker_order_id: str, now: datetime
    ) -> None:
        assert command_id == self.command.id
        self.accepted_broker_order_id = broker_order_id
        self.durable_status = "ACCEPTED"

    async def record_rejected(self, *, command_id: UUID, now: datetime) -> None:
        assert command_id == self.command.id
        del now
        self.rejected_recorded = True
        self.durable_status = "REJECTED"

    async def record_recovery_attempt(self, *, command_id: UUID, now: datetime) -> None:
        assert command_id == self.command.id
        del now
        self.recovery_attempts += 1

    async def command_for_recovery(self, *, command_id: UUID) -> BrokerOrderCommand:
        assert command_id == self.command.id
        return self.command

    async def recoverable_command(
        self, *, command_id: UUID
    ) -> BrokerOrderCommand | None:
        assert command_id == self.command.id
        return self.command if self.attempt_recorded else None


def command() -> BrokerOrderCommand:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    return BrokerOrderCommand(
        id=uuid7(),
        order_id=uuid7(),
        account_id=uuid7(),
        instrument_id=uuid7(),
        command_type=CommandType.SUBMIT,
        target_aggregate_version=1,
        idempotency_key=f"submit:{uuid7()}",
        command_sequence=1,
        canonical_payload_hash=b"p" * 32,
        broker_client_order_id=f"client-{uuid7().hex}",
        target_broker_order_id=None,
        replaces_command_id=None,
        origin_type="STRATEGY",
        authority_class="SUBMIT_NEW_EXPOSURE",
        owner_runtime_instance_id=uuid7(),
        fencing_token=1,
        not_after=now + timedelta(minutes=1),
        side=Side.BUY,
        order_style=OrderStyle.MARKET,
        quantity=Decimal("1"),
        limit_price=None,
        time_in_force="DAY",
    )


def cancel_command() -> BrokerOrderCommand:
    return replace(
        command(),
        command_type=CommandType.CANCEL,
        idempotency_key=f"cancel:{uuid7()}",
        authority_class="CANCEL",
        target_broker_order_id="fake-existing-order",
    )


def replace_command() -> BrokerOrderCommand:
    return replace(
        command(),
        command_type=CommandType.REPLACE,
        idempotency_key=f"replace:{uuid7()}",
        authority_class="REPLACE",
        target_broker_order_id="fake-existing-order",
        replaces_command_id=uuid7(),
    )


def test_dispatch_store_unknown_contract_requires_deadline_and_cooperation() -> None:
    parameters = signature(DispatchStore.record_unknown).parameters

    assert tuple(parameters) == ("self", "command_id", "now", "deadline")
    assert parameters["deadline"].annotation == "datetime"
    contract = getdoc(DispatchStore.record_unknown)
    assert contract is not None
    assert "cancellation-cooperative" in contract
    assert "ACCEPTED" in contract
    assert "idempotent" in contract


@pytest.mark.asyncio
async def test_late_unknown_cannot_downgrade_accepted_fake_store() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    request = command()
    store = MemoryDispatchStore(request, attempt_recorded=True)

    await store.record_accepted(
        command_id=request.id,
        broker_order_id="fake-accepted",
        now=now,
    )
    await store.record_unknown(
        command_id=request.id,
        now=now,
        deadline=request.not_after,
    )

    assert store.durable_status == "ACCEPTED"
    assert store.accepted_broker_order_id == "fake-accepted"
    assert store.unknown_recorded is False


@pytest.mark.asyncio
async def test_timeout_after_accept_uses_provider_recovery_without_second_submit() -> (
    None
):
    now = datetime(2026, 8, 9, tzinfo=UTC)
    request = command()
    store = MemoryDispatchStore(request)
    broker = FakeBroker(scenario=FakeBrokerScenario.TIMEOUT_AFTER_ACCEPT)
    service = DispatchService(store=store, broker=broker)

    await service.dispatch(command_id=request.id, now=now)
    await service.dispatch(command_id=request.id, now=now)

    assert store.attempt_recorded is True
    assert store.unknown_recorded is True
    assert store.accepted_broker_order_id is not None
    assert store.recovery_attempts == 1
    assert broker.submit_count == 1
    assert broker.lookup_count == 1


@pytest.mark.asyncio
async def test_recovery_delegates_the_persisted_attempt_to_provider_once() -> None:
    attempted_at = datetime(2026, 8, 9, tzinfo=UTC)
    now = attempted_at + timedelta(seconds=10)
    request = replace(
        command(),
        dispatch_attempted_at=attempted_at,
        not_after=attempted_at + timedelta(minutes=1),
    )
    store = MemoryDispatchStore(
        request,
        durable_status="UNKNOWN",
        attempt_recorded=True,
        unknown_recorded=True,
    )

    class RecoveryBroker:
        def __init__(self) -> None:
            self.calls: list[tuple[BrokerOrderCommand, datetime]] = []

        async def recover_submit(
            self, supplied: BrokerOrderCommand, *, now: datetime
        ) -> object | None:
            self.calls.append((supplied, now))
            return None

    broker = RecoveryBroker()

    await DispatchService(store=store, broker=broker).recover(
        command_id=request.id,
        now=now,
    )

    assert broker.calls == [(request, now)]
    assert store.recovery_attempts == 1
    assert store.durable_status == "UNKNOWN"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ("missing_attempt", "non_utc_attempt", "future_attempt", "expired", "non_utc_now"),
)
async def test_invalid_recovery_time_never_reaches_the_broker(case: str) -> None:
    attempted_at = datetime(2026, 8, 9, tzinfo=UTC)
    now = attempted_at + timedelta(seconds=10)
    request = replace(
        command(),
        dispatch_attempted_at=attempted_at,
        not_after=attempted_at + timedelta(minutes=1),
    )
    supplied_now = now
    if case == "missing_attempt":
        request = replace(request, dispatch_attempted_at=None)
    elif case == "non_utc_attempt":
        request = replace(
            request,
            dispatch_attempted_at=attempted_at.astimezone(timezone(timedelta(hours=9))),
        )
    elif case == "future_attempt":
        request = replace(request, dispatch_attempted_at=now + timedelta(seconds=1))
    elif case == "expired":
        request = replace(request, not_after=now)
    else:
        supplied_now = now.astimezone(timezone(timedelta(hours=9)))

    store = MemoryDispatchStore(
        request,
        durable_status="UNKNOWN",
        attempt_recorded=True,
        unknown_recorded=True,
    )

    class RecoveryBroker:
        def __init__(self) -> None:
            self.calls = 0

        async def recover_submit(
            self, supplied: BrokerOrderCommand, *, now: datetime
        ) -> object | None:
            del supplied, now
            self.calls += 1
            return None

    broker = RecoveryBroker()

    await DispatchService(store=store, broker=broker).recover(
        command_id=request.id,
        now=supplied_now,
    )

    assert broker.calls == 0
    assert store.recovery_attempts == 0
    assert store.durable_status == "UNKNOWN"


@pytest.mark.asyncio
async def test_denied_or_already_attempted_command_never_calls_broker() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    request = command()
    store = MemoryDispatchStore(request, attempt_recorded=True)
    broker = FakeBroker(scenario=FakeBrokerScenario.FULL_FILL)

    await DispatchService(store=store, broker=broker).dispatch(
        command_id=request.id, now=now
    )

    assert broker.submit_count == 0


@pytest.mark.asyncio
async def test_authorization_failure_writes_no_dispatch_state() -> None:
    class FailingAuthorizationStore(MemoryDispatchStore):
        async def authorize_and_record_attempt(
            self, *, command_id: UUID, now: datetime
        ) -> BrokerOrderCommand | None:
            assert command_id == self.command.id
            del now
            raise RuntimeError("authorization transaction failed")

    now = datetime(2026, 8, 9, tzinfo=UTC)
    request = command()
    store = FailingAuthorizationStore(request)
    broker = FakeBroker(scenario=FakeBrokerScenario.FULL_FILL)

    with pytest.raises(RuntimeError, match="authorization transaction failed"):
        await DispatchService(store=store, broker=broker).dispatch(
            command_id=request.id, now=now
        )

    assert store.attempt_recorded is False
    assert store.durable_status == "PENDING"
    assert store.unknown_attempts == 0
    assert store.accepted_broker_order_id is None
    assert broker.submit_count == 0


@pytest.mark.asyncio
async def test_unexpected_broker_failure_is_retained_as_unknown() -> None:
    class BrokenBroker:
        async def submit(self, _: BrokerOrderCommand) -> object:
            raise ConnectionError("connection reset")

        async def find_by_client_order_id(self, _: str) -> object | None:
            return None

    now = datetime(2026, 8, 9, tzinfo=UTC)
    request = command()
    store = MemoryDispatchStore(request)

    await DispatchService(store=store, broker=BrokenBroker()).dispatch(
        command_id=request.id, now=now
    )

    assert store.attempt_recorded is True
    assert store.unknown_recorded is True


@pytest.mark.asyncio
async def test_authoritative_broker_rejection_is_not_retained_as_unknown() -> None:
    class RejectingBroker:
        async def submit(self, _: BrokerOrderCommand) -> object:
            raise BrokerSubmissionRejected("provider rejected the request")

    now = datetime(2026, 8, 9, tzinfo=UTC)
    request = command()
    store = MemoryDispatchStore(request)

    await DispatchService(store=store, broker=RejectingBroker()).dispatch(
        command_id=request.id, now=now
    )

    assert store.rejected_recorded is True
    assert store.unknown_recorded is False
    assert store.durable_status == "REJECTED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_factory", "expected_calls"),
    (
        (command, (1, 0, 0)),
        (cancel_command, (0, 1, 0)),
        (replace_command, (0, 0, 1)),
    ),
)
async def test_acknowledgement_failure_records_unknown_without_resubmitting(
    request_factory: Callable[[], BrokerOrderCommand],
    expected_calls: tuple[int, int, int],
) -> None:
    class BrokenAcknowledgement:
        @property
        def broker_order_id(self) -> str:
            raise ValueError("invalid broker acknowledgement")

    class Broker:
        def __init__(self) -> None:
            self.submit_count = 0
            self.cancel_count = 0
            self.replace_count = 0
            self.recovery_count = 0

        async def submit(self, _: BrokerOrderCommand) -> object:
            self.submit_count += 1
            return BrokenAcknowledgement()

        async def cancel(self, _: BrokerOrderCommand) -> object:
            self.cancel_count += 1
            return BrokenAcknowledgement()

        async def replace(self, _: BrokerOrderCommand) -> object:
            self.replace_count += 1
            return BrokenAcknowledgement()

        async def recover_submit(
            self, _: BrokerOrderCommand, *, now: datetime
        ) -> object | None:
            del now
            self.recovery_count += 1
            return None

    now = datetime(2026, 8, 9, tzinfo=UTC)
    request = request_factory()
    store = MemoryDispatchStore(request)
    broker = Broker()
    service = DispatchService(store=store, broker=broker)

    await service.dispatch(command_id=request.id, now=now)
    await service.dispatch(command_id=request.id, now=now)

    assert store.attempt_recorded is True
    assert store.unknown_recorded is True
    assert store.accepted_broker_order_id is None
    assert (
        broker.submit_count,
        broker.cancel_count,
        broker.replace_count,
    ) == expected_calls
    assert broker.recovery_count == int(request.command_type is CommandType.SUBMIT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    (
        asyncio.CancelledError("private-cancel"),
        KeyboardInterrupt("private-interrupt"),
        SystemExit("private-exit"),
    ),
)
async def test_post_attempt_control_records_unknown_then_reraises_sanitized_identity(
    error: BaseException,
) -> None:
    class ControlBroker:
        def __init__(self) -> None:
            self.submit_count = 0

        async def submit(self, _: BrokerOrderCommand) -> object:
            self.submit_count += 1
            raise error

        async def find_by_client_order_id(self, _: str) -> object | None:
            return None

    now = datetime(2026, 8, 9, tzinfo=UTC)
    request = command()
    store = MemoryDispatchStore(request)
    broker = ControlBroker()

    with pytest.raises(type(error)) as caught:
        await DispatchService(store=store, broker=broker).dispatch(
            command_id=request.id, now=now
        )

    assert caught.value is error
    assert caught.value.args == ()
    assert caught.value.__dict__ == {}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    if isinstance(caught.value, SystemExit):
        assert caught.value.code == 1
    assert store.attempt_recorded is True
    assert store.unknown_recorded is True
    assert broker.submit_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    (
        asyncio.CancelledError("private-cancel"),
        KeyboardInterrupt("private-interrupt"),
        SystemExit("private-exit"),
    ),
)
async def test_unknown_persistence_failure_cannot_replace_original_control(
    error: BaseException,
) -> None:
    class FailingUnknownStore(MemoryDispatchStore):
        async def record_unknown(
            self, *, command_id: UUID, now: datetime, deadline: datetime
        ) -> None:
            assert command_id == self.command.id
            assert deadline == self.command.not_after
            del now, deadline
            self.unknown_attempts += 1
            raise RuntimeError("unknown persistence failed")

    class ControlBroker:
        def __init__(self) -> None:
            self.submit_count = 0
            self.recovery_count = 0

        async def submit(self, _: BrokerOrderCommand) -> object:
            self.submit_count += 1
            raise error

        async def recover_submit(
            self, _: BrokerOrderCommand, *, now: datetime
        ) -> object | None:
            del now
            self.recovery_count += 1
            return None

    now = datetime(2026, 8, 9, tzinfo=UTC)
    request = command()
    store = FailingUnknownStore(request)
    broker = ControlBroker()
    service = DispatchService(store=store, broker=broker)

    with pytest.raises(type(error)) as caught:
        await service.dispatch(command_id=request.id, now=now)

    assert caught.value is error
    assert caught.value.args == ()
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert store.attempt_recorded is True
    assert (await store.recoverable_command(command_id=request.id)).id == request.id
    assert store.durable_status == "DISPATCHING"
    assert store.unknown_recorded is False

    with pytest.raises(RuntimeError, match="unknown persistence failed"):
        await service.dispatch(command_id=request.id, now=now)

    assert broker.submit_count == 1
    assert broker.recovery_count == 1


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_unknown_persistence() -> None:
    original = asyncio.CancelledError("private-initial-cancel")

    class BlockingUnknownStore(MemoryDispatchStore):
        def __init__(self, request: BrokerOrderCommand) -> None:
            super().__init__(request)
            self.unknown_started = asyncio.Event()
            self.allow_unknown = asyncio.Event()

        async def record_unknown(
            self, *, command_id: UUID, now: datetime, deadline: datetime
        ) -> None:
            assert command_id == self.command.id
            assert deadline == self.command.not_after
            del now, deadline
            self.unknown_attempts += 1
            self.unknown_started.set()
            await self.allow_unknown.wait()
            self.unknown_recorded = True

    class ControlBroker:
        async def submit(self, _: BrokerOrderCommand) -> object:
            raise original

        async def find_by_client_order_id(self, _: str) -> object | None:
            return None

    now = datetime(2026, 8, 9, tzinfo=UTC)
    request = command()
    store = BlockingUnknownStore(request)
    dispatch_task = asyncio.create_task(
        DispatchService(store=store, broker=ControlBroker()).dispatch(
            command_id=request.id, now=now
        )
    )

    await asyncio.wait_for(store.unknown_started.wait(), timeout=0.5)
    dispatch_task.cancel()
    await asyncio.sleep(0)
    store.allow_unknown.set()

    with pytest.raises(asyncio.CancelledError) as caught:
        await dispatch_task

    assert caught.value is original
    assert store.unknown_recorded is True


@pytest.mark.asyncio
async def test_cooperative_unknown_persistence_stops_at_command_deadline() -> None:
    original = asyncio.CancelledError("private-initial-cancel")

    class CooperativeUnknownStore(MemoryDispatchStore):
        def __init__(self, request: BrokerOrderCommand) -> None:
            super().__init__(request)
            self.first_unknown_started = asyncio.Event()
            self.first_unknown_cancelled = False
            self.allow_first_unknown = asyncio.Event()

        async def record_unknown(
            self, *, command_id: UUID, now: datetime, deadline: datetime
        ) -> None:
            assert command_id == self.command.id
            assert deadline == self.command.not_after
            if self.unknown_attempts == 0:
                self.unknown_attempts = 1
                self.first_unknown_started.set()
                try:
                    await self.allow_first_unknown.wait()
                finally:
                    self.first_unknown_cancelled = True
                return
            await super().record_unknown(
                command_id=command_id,
                now=now,
                deadline=deadline,
            )

    class ControlBroker:
        def __init__(self) -> None:
            self.cancel_count = 0

        async def cancel(self, _: BrokerOrderCommand) -> object:
            self.cancel_count += 1
            raise original

    now = datetime(2026, 8, 9, tzinfo=UTC)
    request = replace(cancel_command(), not_after=now + timedelta(milliseconds=50))
    store = CooperativeUnknownStore(request)
    broker = ControlBroker()
    service = DispatchService(store=store, broker=broker)
    dispatch_task = asyncio.create_task(
        service.dispatch(command_id=request.id, now=now)
    )

    await asyncio.wait_for(store.first_unknown_started.wait(), timeout=0.5)
    started_at = asyncio.get_running_loop().time()
    for _ in range(3):
        dispatch_task.cancel()
        await asyncio.sleep(0)
    try:
        done, _ = await asyncio.wait((dispatch_task,), timeout=0.5)
        assert dispatch_task in done
        with pytest.raises(asyncio.CancelledError) as caught:
            await dispatch_task
    finally:
        if not dispatch_task.done():
            store.allow_first_unknown.set()
            with pytest.raises(asyncio.CancelledError):
                await dispatch_task

    assert caught.value is original
    assert caught.value.args == ()
    assert asyncio.get_running_loop().time() - started_at < 0.4
    assert store.unknown_recorded is False
    assert store.durable_status == "DISPATCHING"
    assert store.first_unknown_cancelled is True
    current = asyncio.current_task()
    assert not [
        task for task in asyncio.all_tasks() if task is not current and not task.done()
    ]

    await service.dispatch(command_id=request.id, now=now)

    assert store.unknown_recorded is True
    assert broker.cancel_count == 1


@pytest.mark.parametrize("cleanup_control", ("KeyboardInterrupt", "SystemExit"))
def test_cleanup_control_cannot_escape_child_task_under_asyncio_run(
    cleanup_control: str,
) -> None:
    source = f"""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from autotrader.execution.dispatch.service import DispatchService
from autotrader.execution.orders.models import CommandType

now = datetime(2026, 8, 9, tzinfo=UTC)
command_id = uuid4()
original = asyncio.CancelledError("private-original-control")

class Store:
    async def authorize_and_record_attempt(self, *, command_id, now):
        return SimpleNamespace(
            id=command_id,
            not_after=now + timedelta(seconds=1),
            command_type=CommandType.CANCEL,
        )

    async def record_unknown(self, *, command_id, now, deadline):
        raise {cleanup_control}("private-cleanup-control")

class Broker:
    async def cancel(self, command):
        raise original

async def main():
    try:
        await DispatchService(store=Store(), broker=Broker()).dispatch(
            command_id=command_id,
            now=now,
        )
    except asyncio.CancelledError as caught:
        assert caught is original
        assert caught.args == ()
        assert caught.__dict__ == {{}}
        assert caught.__context__ is None
        assert caught.__cause__ is None
        print("ORIGINAL_CONTROL_PRESERVED=1")
        return
    raise AssertionError("original control did not propagate")

asyncio.run(main())
"""

    completed = subprocess.run(
        [sys.executable, "-c", source],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "ORIGINAL_CONTROL_PRESERVED=1\n"
    assert "private" not in completed.stdout + completed.stderr


def test_cooperative_unknown_is_joined_before_asyncio_run_returns() -> None:
    source = """
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from autotrader.execution.dispatch.service import DispatchService
from autotrader.execution.orders.models import CommandType

now = datetime(2026, 8, 9, tzinfo=UTC)
command_id = uuid4()
original = asyncio.CancelledError("private-original-control")

class Store:
    def __init__(self):
        self.finished = False

    async def authorize_and_record_attempt(self, *, command_id, now):
        return SimpleNamespace(
            id=command_id,
            not_after=now + timedelta(milliseconds=50),
            command_type=CommandType.CANCEL,
        )

    async def record_unknown(self, *, command_id, now, deadline):
        try:
            await asyncio.Event().wait()
        finally:
            self.finished = True

class Broker:
    async def cancel(self, command):
        raise original

async def main():
    store = Store()
    try:
        await DispatchService(store=store, broker=Broker()).dispatch(
            command_id=command_id,
            now=now,
        )
    except asyncio.CancelledError as caught:
        assert caught is original
        assert caught.args == ()
        assert store.finished is True
        current = asyncio.current_task()
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]
        print("COOPERATIVE_UNKNOWN_JOINED=1")
        return
    raise AssertionError("original control did not propagate")

asyncio.run(main())
"""

    completed = subprocess.run(
        [sys.executable, "-c", source],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "COOPERATIVE_UNKNOWN_JOINED=1\n"
    assert "private" not in completed.stdout + completed.stderr


@pytest.mark.asyncio
async def test_cancel_uses_cancel_boundary_without_creating_a_submit() -> None:
    class CancelBroker:
        submit_count = 0
        cancel_count = 0

        async def submit(self, _: BrokerOrderCommand) -> object:
            self.submit_count += 1
            raise AssertionError("cancel must not use submit")

        async def cancel(self, request: BrokerOrderCommand) -> object:
            self.cancel_count += 1
            assert request.target_broker_order_id == "fake-existing-order"
            return type("Accepted", (), {"broker_order_id": "fake-existing-order"})()

        async def find_by_client_order_id(self, _: str) -> object | None:
            return None

    now = datetime(2026, 8, 9, tzinfo=UTC)
    request = cancel_command()
    store = MemoryDispatchStore(request)
    broker = CancelBroker()

    await DispatchService(store=store, broker=broker).dispatch(
        command_id=request.id, now=now
    )

    assert broker.submit_count == 0
    assert broker.cancel_count == 1
    assert store.accepted_broker_order_id == "fake-existing-order"


@pytest.mark.asyncio
async def test_cancel_recovery_is_unknown_without_a_second_broker_call() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    request = cancel_command()
    store = MemoryDispatchStore(request, attempt_recorded=True)
    broker = FakeBroker(scenario=FakeBrokerScenario.FULL_FILL)

    await DispatchService(store=store, broker=broker).dispatch(
        command_id=request.id, now=now
    )

    assert store.unknown_recorded is True
    assert broker.submit_count == 0
    assert broker.cancel_count == 0
