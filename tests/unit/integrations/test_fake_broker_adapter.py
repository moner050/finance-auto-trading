from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid7

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.fake.adapter import (
    FakeBroker,
    FakeBrokerEmission,
    FakeBrokerProcessCrash,
    FakeBrokerScenario,
    FakeBrokerTimeout,
)


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


@pytest.mark.asyncio
async def test_timeout_after_accept_has_stable_lookup_without_second_submit() -> None:
    broker = FakeBroker(scenario=FakeBrokerScenario.TIMEOUT_AFTER_ACCEPT)
    request = command()

    with pytest.raises(FakeBrokerTimeout):
        await broker.submit(request)

    recovered = await broker.find_by_client_order_id(request.broker_client_order_id)
    assert recovered is not None
    assert recovered.broker_client_order_id == request.broker_client_order_id
    assert broker.submit_count == 1
    assert broker.lookup_count == 1


@pytest.mark.asyncio
async def test_duplicate_submit_is_idempotent_at_broker_boundary() -> None:
    broker = FakeBroker(scenario=FakeBrokerScenario.FULL_FILL)
    request = command()

    first = await broker.submit(request)
    second = await broker.submit(request)

    assert first == second
    assert broker.submit_count == 1


@pytest.mark.asyncio
async def test_process_crash_follows_broker_acceptance_without_returning() -> None:
    broker = FakeBroker(
        scenario=FakeBrokerScenario.ACCEPT_THEN_CRASH_BEFORE_RESULT_COMMIT
    )
    request = command()

    with pytest.raises(FakeBrokerProcessCrash):
        await broker.submit(request)

    assert broker.submit_count == 1
    assert (
        await broker.find_by_client_order_id(request.broker_client_order_id) is not None
    )


@pytest.mark.asyncio
async def test_reversed_status_scenario_is_a_replayable_external_event_plan() -> None:
    broker = FakeBroker(scenario=FakeBrokerScenario.REVERSED_STATUS_EVENTS)
    request = command()
    await broker.submit(request)

    assert broker.emissions_for(request.broker_client_order_id) == (
        FakeBrokerEmission("FILLED", 2),
        FakeBrokerEmission("ACKNOWLEDGED", 1),
    )
