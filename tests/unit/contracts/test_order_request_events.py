from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest
from pydantic import ValidationError

from autotrader.contracts.broker_events import (
    BrokerExecutionCheckpointPayload,
    BrokerOrderStatusPayload,
)
from autotrader.contracts.order_request_events import (
    ExecutionOrderRequestPayload,
    OrderRequestOrigin,
)


def test_protection_request_requires_only_protection_evidence() -> None:
    payload = ExecutionOrderRequestPayload(
        origin=OrderRequestOrigin.PROTECTION,
        protection_position_id=uuid7(),
        operator_audit_id=None,
        reconciliation_diff_id=None,
    )

    assert payload.origin is OrderRequestOrigin.PROTECTION


@pytest.mark.parametrize(
    ("origin", "protection_position_id", "operator_audit_id", "reconciliation_diff_id"),
    [
        (OrderRequestOrigin.PROTECTION, None, None, None),
        (OrderRequestOrigin.PROTECTION, uuid7(), uuid7(), None),
        (OrderRequestOrigin.OPERATOR, uuid7(), uuid7(), None),
        (OrderRequestOrigin.RECONCILIATION, None, None, None),
    ],
)
def test_request_rejects_missing_or_ambiguous_or_disabled_evidence(
    origin: OrderRequestOrigin,
    protection_position_id: object,
    operator_audit_id: object,
    reconciliation_diff_id: object,
) -> None:
    with pytest.raises(ValidationError):
        ExecutionOrderRequestPayload(
            origin=origin,
            protection_position_id=protection_position_id,
            operator_audit_id=operator_audit_id,
            reconciliation_diff_id=reconciliation_diff_id,
        )


def test_broker_status_payload_requires_stable_broker_identities() -> None:
    payload = BrokerOrderStatusPayload(
        broker_id=uuid7(),
        account_id=uuid7(),
        source_partition="orders",
        dedupe_key="broker-event-1",
        broker_order_id="broker-order-1",
        broker_client_order_id="client-order-1",
        raw_status="CANCELED",
        requested_quantity="1",
        cumulative_filled_quantity="0",
        source_sequence=1,
    )

    assert payload.source_sequence == 1


@pytest.mark.parametrize("dedupe_key", ["", "한글"])
def test_broker_status_payload_rejects_unstable_dedupe_identity(
    dedupe_key: str,
) -> None:
    with pytest.raises(ValidationError):
        BrokerOrderStatusPayload(
            broker_id=uuid7(),
            account_id=uuid7(),
            source_partition="orders",
            dedupe_key=dedupe_key,
            broker_order_id="broker-order-1",
            broker_client_order_id="client-order-1",
            raw_status="CANCELED",
            requested_quantity="1",
            cumulative_filled_quantity="0",
        )


def test_complete_checkpoint_requires_exact_scope_and_closed_fresh_interval() -> None:
    now = datetime(2026, 8, 9, tzinfo=UTC)
    payload = BrokerExecutionCheckpointPayload(
        broker_id=uuid7(),
        account_id=uuid7(),
        source_partition="execution-history",
        broker_order_ids=("broker-order-1",),
        broker_client_order_ids=("client-order-1",),
        covered_from_at=now - timedelta(minutes=1),
        covered_through_at=now,
        pagination_complete=True,
        has_gap=False,
        expires_at=now + timedelta(minutes=1),
        query_fingerprint_hex="a" * 64,
    )

    assert payload.pagination_complete is True


def test_checkpoint_rejects_non_ascii_scope_or_naive_timestamp() -> None:
    now = datetime(2026, 8, 9)
    with pytest.raises(ValidationError):
        BrokerExecutionCheckpointPayload(
            broker_id=uuid7(),
            account_id=uuid7(),
            source_partition="execution-history",
            broker_order_ids=("한글",),
            covered_from_at=now,
            covered_through_at=now,
            pagination_complete=True,
            has_gap=False,
            expires_at=now,
            query_fingerprint_hex="a" * 64,
        )
