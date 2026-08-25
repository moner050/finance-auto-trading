from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import BaseModel

from autotrader.contracts.envelope import EventEnvelope
from autotrader.shared.decimal import ContractDecimal, NonNegativeDecimal


class PricePayload(BaseModel):
    price: ContractDecimal
    quantity: NonNegativeDecimal
    raw_external_status: str


def build_envelope() -> EventEnvelope[PricePayload]:
    return EventEnvelope(
        event_id="01989400-0000-7000-8000-000000000001",
        event_type="test.price.v1",
        schema_version=1,
        occurred_at=datetime(2026, 8, 9, tzinfo=UTC),
        observed_at=datetime(2026, 8, 9, tzinfo=UTC),
        producer="test",
        partition_key="instrument:example",
        aggregate_type="test_aggregate",
        aggregate_id="01989400-0000-7000-8000-000000000002",
        aggregate_version=1,
        correlation_id="01989400-0000-7000-8000-000000000003",
        causation_id=None,
        trace_id="trace-1",
        payload=PricePayload(
            price=Decimal("12.30"),
            quantity=Decimal("1"),
            raw_external_status="NEW_CODE",
        ),
    )


def test_event_envelope_has_deterministic_canonical_bytes_and_hash() -> None:
    first = build_envelope()
    second = build_envelope()

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.sha256() == second.sha256()
    assert b'"price":"12.30"' in first.canonical_bytes()
    assert b'"raw_external_status":"NEW_CODE"' in first.canonical_bytes()


def test_event_envelope_rejects_naive_timestamps() -> None:
    values = build_envelope().model_dump()
    values["occurred_at"] = datetime(2026, 8, 9)

    with pytest.raises(ValueError, match="timezone-aware UTC"):
        EventEnvelope[PricePayload](**values)


def test_event_envelope_rejects_non_v7_identity() -> None:
    values = build_envelope().model_dump()
    values["event_id"] = "5f3c9513-0f5b-4c26-8015-c69bf7c8a216"

    with pytest.raises(ValueError, match="UUIDv7"):
        EventEnvelope[PricePayload](**values)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("price", 1.1, "float"),
        ("quantity", "-1", "non-negative"),
    ],
)
def test_event_envelope_rejects_float_and_negative_payload_values(
    field: str, value: object, message: str
) -> None:
    values = build_envelope().model_dump()
    values["payload"][field] = value

    with pytest.raises(ValueError, match=message):
        EventEnvelope[PricePayload](**values)
