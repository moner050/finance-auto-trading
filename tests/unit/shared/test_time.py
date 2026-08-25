from datetime import UTC, datetime, timedelta, timezone

import pytest

from autotrader.shared.time import require_utc


def test_require_utc_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        require_utc(datetime(2026, 8, 9))


def test_require_utc_normalizes_an_aware_datetime() -> None:
    value = datetime(2026, 8, 9, 9, tzinfo=timezone(timedelta(hours=9)))

    assert require_utc(value) == datetime(2026, 8, 9, tzinfo=UTC)
