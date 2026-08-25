from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from autotrader.shared.ids import (
    new_uuid7,
    uuid7_from_sha256,
    uuid_from_binary,
    uuid_to_binary,
)


def test_uuid7_binary_round_trip_preserves_identity() -> None:
    value = new_uuid7()

    assert uuid_from_binary(uuid_to_binary(value)) == value


def test_new_uuid7_values_are_chronological() -> None:
    first = new_uuid7()
    second = new_uuid7()

    assert first.int < second.int


def test_deterministic_uuid7_separates_digest_and_timestamp_inputs() -> None:
    timestamp = datetime(2026, 8, 20, 1, 0, tzinfo=UTC)
    first = uuid7_from_sha256(timestamp, b"a" * 32)
    retry = uuid7_from_sha256(timestamp, b"a" * 32)
    changed_digest = uuid7_from_sha256(timestamp, b"b" * 32)
    changed_time = uuid7_from_sha256(timestamp + timedelta(seconds=1), b"a" * 32)

    assert retry == first
    assert len({first, changed_digest, changed_time}) == 3
    assert all(value.version == 7 for value in (first, changed_digest, changed_time))


@pytest.mark.parametrize("value", [uuid4(), UUID(int=0)])
def test_uuid_binary_helpers_reject_non_v7_values(value: UUID) -> None:
    with pytest.raises(ValueError, match="UUIDv7"):
        uuid_to_binary(value)
