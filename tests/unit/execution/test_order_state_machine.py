from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid7

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.intents.models import IntentOrigin
from autotrader.execution.orders.models import (
    BrokerOrderLinkState,
    BrokerOrderStatusEvent,
    BrokerStatusWatermark,
    CommandType,
    DeferredBrokerStatus,
    Order,
    OrderStatus,
    all_exposure_links_terminal,
)
from autotrader.execution.orders.service import (
    OrderCommandFactory,
    OrderSubmissionContext,
)
from autotrader.execution.orders.state_machine import (
    InvalidOrderTransitionError,
    OrderStateMachine,
)


def order() -> Order:
    return Order(
        id=uuid7(),
        order_intent_id=uuid7(),
        risk_decision_id=uuid7(),
        account_id=uuid7(),
        instrument_id=uuid7(),
        side=Side.BUY,
        order_style=OrderStyle.LIMIT,
        requested_quantity=Decimal("2"),
        limit_price=Decimal("10"),
        status=OrderStatus.CREATED,
        aggregate_version=0,
        broker_client_order_id="order-client-id",
        created_at=datetime(2026, 8, 9, tzinfo=UTC),
    )


def submission_context() -> OrderSubmissionContext:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    return OrderSubmissionContext(
        broker_client_order_id="order-client-id",
        owner_runtime_instance_id=uuid7(),
        fencing_token=1,
        not_after=now.replace(hour=1),
        time_in_force="DAY",
        authority_class="SUBMIT_NEW_EXPOSURE",
        created_at=now,
    )


def test_accepted_statuses_advance_canonical_version_exactly_once() -> None:
    state_machine = OrderStateMachine()
    current = order()
    for expected, external in (
        (OrderStatus.SUBMITTED, "SUBMITTED"),
        (OrderStatus.ACKNOWLEDGED, "ACKNOWLEDGED"),
        (OrderStatus.PARTIALLY_FILLED, "PARTIALLY_FILLED"),
        (OrderStatus.FILLED, "FILLED"),
    ):
        status_event = BrokerOrderStatusEvent(
            broker_id=uuid7(),
            account_id=current.account_id,
            source_partition="status",
            dedupe_key=uuid7().hex,
            raw_status=external,
            occurred_at=datetime(2026, 8, 9, tzinfo=UTC),
            broker_order_id="order-1" if external == "FILLED" else None,
        )
        transition = state_machine.apply(
            current,
            status_event,
            links=(
                BrokerOrderLinkState(
                    id=uuid7(),
                    broker_order_id="order-1",
                    link_sequence=1,
                    exposure_bearing=True,
                    status=OrderStatus.ACKNOWLEDGED,
                ),
            )
            if external == "FILLED"
            else None,
        )
        current = transition.order
        assert current.status is expected
        assert current.aggregate_version == transition.event.aggregate_version


def test_unknown_external_status_maps_to_unknown_without_guessing() -> None:
    current = order()
    transition = OrderStateMachine().apply(
        current,
        BrokerOrderStatusEvent(
            broker_id=uuid7(),
            account_id=current.account_id,
            source_partition="status",
            dedupe_key=uuid7().hex,
            raw_status="BROKER_UNDOCUMENTED_STATE",
            occurred_at=datetime(2026, 8, 9, tzinfo=UTC),
        ),
    )

    assert transition.order.status is OrderStatus.UNKNOWN
    assert transition.event.aggregate_version == 1


def test_invalid_backward_transition_is_rejected_without_new_event() -> None:
    with pytest.raises(InvalidOrderTransitionError):
        OrderStateMachine().apply(
            replace(order(), status=OrderStatus.FILLED),
            BrokerOrderStatusEvent(
                broker_id=uuid7(),
                account_id=uuid7(),
                source_partition="status",
                dedupe_key=uuid7().hex,
                raw_status="ACKNOWLEDGED",
                occurred_at=datetime(2026, 8, 9, tzinfo=UTC),
            ),
        )


def test_command_identity_is_order_type_and_target_version_scoped() -> None:
    current = order()
    factory = OrderCommandFactory()

    submission = replace(submission_context(), authority_class="CANCEL")
    first = factory.create(
        order=current,
        command_type=CommandType.CANCEL,
        submission=submission,
        origin=IntentOrigin.STRATEGY,
        target_broker_order_id="broker-order-a",
    )
    duplicate = factory.create(
        order=current,
        command_type=CommandType.CANCEL,
        submission=submission,
        origin=IntentOrigin.STRATEGY,
        target_broker_order_id="broker-order-a",
    )
    conflicting_target = factory.create(
        order=current,
        command_type=CommandType.CANCEL,
        submission=submission,
        origin=IntentOrigin.STRATEGY,
        target_broker_order_id="broker-order-b",
    )
    later = factory.create(
        order=replace(current, aggregate_version=current.aggregate_version + 1),
        command_type=CommandType.CANCEL,
        submission=submission,
        origin=IntentOrigin.STRATEGY,
    )

    assert first.idempotency_key == duplicate.idempotency_key
    assert first.idempotency_key == conflicting_target.idempotency_key
    assert first.canonical_payload_hash != conflicting_target.canonical_payload_hash
    assert first.idempotency_key != later.idempotency_key
    assert first.command_sequence == current.aggregate_version
    assert later.command_sequence == current.aggregate_version + 1


def test_future_broker_sequence_is_deferred_without_domain_effect() -> None:
    current = replace(order(), status=OrderStatus.SUBMITTED)
    result = OrderStateMachine().apply(
        current,
        BrokerOrderStatusEvent(
            broker_id=uuid7(),
            account_id=current.account_id,
            source_partition="status",
            dedupe_key=uuid7().hex,
            raw_status="ACKNOWLEDGED",
            occurred_at=datetime(2026, 8, 9, tzinfo=UTC),
            source_sequence=3,
        ),
        watermarks=(BrokerStatusWatermark("status", 1),),
    )

    assert isinstance(result, DeferredBrokerStatus)
    assert result.order == current
    assert result.reason == "SEQUENCE_GAP"
    assert result.missing_from_sequence == 2


def test_terminal_predecessor_does_not_terminalize_live_successor_lineage() -> None:
    links = (
        BrokerOrderLinkState(
            id=uuid7(),
            broker_order_id="predecessor",
            link_sequence=1,
            exposure_bearing=True,
            status=OrderStatus.CANCELED,
        ),
        BrokerOrderLinkState(
            id=uuid7(),
            broker_order_id="successor",
            link_sequence=2,
            exposure_bearing=True,
            status=OrderStatus.ACKNOWLEDGED,
        ),
    )

    current = replace(order(), status=OrderStatus.CANCEL_PENDING)
    result = OrderStateMachine().apply(
        current,
        BrokerOrderStatusEvent(
            broker_id=uuid7(),
            account_id=current.account_id,
            source_partition="status",
            dedupe_key=uuid7().hex,
            raw_status="CANCELED",
            occurred_at=datetime(2026, 8, 9, tzinfo=UTC),
            broker_order_id="predecessor",
        ),
        links=links,
    )

    assert all_exposure_links_terminal(links) is False
    assert isinstance(result, DeferredBrokerStatus)
    assert result.order == current
    assert result.reason == "LIVE_SUCCESSOR_LINK"


def test_linked_broker_cancellation_can_skip_cancel_pending() -> None:
    current = replace(order(), status=OrderStatus.SUBMITTED)
    result = OrderStateMachine().apply(
        current,
        BrokerOrderStatusEvent(
            broker_id=uuid7(),
            account_id=current.account_id,
            source_partition="status",
            dedupe_key=uuid7().hex,
            raw_status="CANCELED",
            occurred_at=datetime(2026, 8, 9, tzinfo=UTC),
            broker_order_id="broker-order",
        ),
        links=(
            BrokerOrderLinkState(
                id=uuid7(),
                broker_order_id="broker-order",
                link_sequence=1,
                exposure_bearing=True,
                status=OrderStatus.SUBMITTED,
            ),
        ),
    )

    assert result.order.status is OrderStatus.CANCELED


def test_late_acknowledgement_after_partial_fill_is_deferred() -> None:
    current = replace(order(), status=OrderStatus.PARTIALLY_FILLED)
    result = OrderStateMachine().apply(
        current,
        BrokerOrderStatusEvent(
            broker_id=uuid7(),
            account_id=current.account_id,
            source_partition="status",
            dedupe_key=uuid7().hex,
            raw_status="ACKNOWLEDGED",
            occurred_at=datetime(2026, 8, 9, tzinfo=UTC),
            source_sequence=1,
        ),
    )

    assert isinstance(result, DeferredBrokerStatus)
    assert result.reason == "STALE_STATUS"


def test_unsequenced_acknowledgement_after_partial_fill_is_rejected() -> None:
    current = replace(order(), status=OrderStatus.PARTIALLY_FILLED)

    with pytest.raises(InvalidOrderTransitionError):
        OrderStateMachine().apply(
            current,
            BrokerOrderStatusEvent(
                broker_id=uuid7(),
                account_id=current.account_id,
                source_partition="status",
                dedupe_key=uuid7().hex,
                raw_status="ACKNOWLEDGED",
                occurred_at=datetime(2026, 8, 9, tzinfo=UTC),
            ),
        )


def test_late_broker_sequence_is_deferred_without_domain_effect() -> None:
    current = replace(order(), status=OrderStatus.ACKNOWLEDGED)
    result = OrderStateMachine().apply(
        current,
        BrokerOrderStatusEvent(
            broker_id=uuid7(),
            account_id=current.account_id,
            source_partition="status",
            dedupe_key=uuid7().hex,
            raw_status="FILLED",
            occurred_at=datetime(2026, 8, 9, tzinfo=UTC),
            source_sequence=2,
        ),
        watermarks=(BrokerStatusWatermark("status", 3),),
    )

    assert isinstance(result, DeferredBrokerStatus)
    assert result.order == current
    assert result.reason == "STALE_SEQUENCE"
    assert result.missing_from_sequence is None


def test_terminal_status_without_locked_lineage_is_deferred_fail_closed() -> None:
    current = replace(order(), status=OrderStatus.ACKNOWLEDGED)
    result = OrderStateMachine().apply(
        current,
        BrokerOrderStatusEvent(
            broker_id=uuid7(),
            account_id=current.account_id,
            source_partition="status",
            dedupe_key=uuid7().hex,
            raw_status="FILLED",
            occurred_at=datetime(2026, 8, 9, tzinfo=UTC),
            broker_order_id="broker-order",
        ),
    )

    assert isinstance(result, DeferredBrokerStatus)
    assert result.order == current
