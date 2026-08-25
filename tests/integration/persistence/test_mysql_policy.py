from datetime import UTC, datetime
from uuid import uuid4, uuid7

import pytest

from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary


def test_uuid_binary_round_trip():
    value = uuid7()

    assert (
        UuidBinary().process_result_value(
            UuidBinary().process_bind_param(value, None), None
        )
        == value
    )


def test_uuid_binary_rejects_non_v7_values():
    value = uuid4()

    with pytest.raises(ValueError, match="UUIDv7"):
        UuidBinary().process_bind_param(value, None)
    with pytest.raises(ValueError, match="UUIDv7"):
        UuidBinary().process_result_value(value.bytes, None)


def test_utc_datetime_rejects_naive():
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        UtcDateTime().process_bind_param(datetime(2026, 8, 6), None)


def test_utc_datetime_normalizes_aware_values_to_utc():
    value = datetime(2026, 8, 6, tzinfo=UTC)

    assert UtcDateTime().process_bind_param(value, None) == value.replace(tzinfo=None)
