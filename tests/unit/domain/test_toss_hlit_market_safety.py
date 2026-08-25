from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from autotrader.domain.toss_hlit_market_safety import (
    TossHlitKrxMarketSafetyEvidence,
    TossHlitKrxMarketSafetySourceEvidence,
)

_OBSERVED_AT = datetime(2026, 8, 12, 6, 20, tzinfo=UTC)


def _valid_source() -> TossHlitKrxMarketSafetySourceEvidence:
    return TossHlitKrxMarketSafetySourceEvidence.from_components(
        evidence=TossHlitKrxMarketSafetyEvidence(
            symbol="005930",
            observed_at=_OBSERVED_AT,
            has_active_krx_vi=True,
            is_single_price_auction=True,
        ),
        vi_source_id=UUID("018f27e6-3b4c-7a10-8123-123456789abc"),
        vi_source_hash=b"v" * 32,
        vi_expires_at=_OBSERVED_AT + timedelta(minutes=1),
        calendar_source_id=UUID("018f27e6-3b4c-7a10-8123-123456789abd"),
        calendar_source_hash=b"c" * 32,
        calendar_expires_at=_OBSERVED_AT + timedelta(minutes=2),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("evidence", object()),
        ("vi_source_id", "not-a-uuid"),
        ("vi_source_hash", b"v" * 31),
        ("vi_expires_at", _OBSERVED_AT),
        ("calendar_source_id", UUID(int=0)),
        ("calendar_source_hash", bytearray(b"c" * 32)),
        ("calendar_expires_at", _OBSERVED_AT.replace(microsecond=1)),
        ("source_hash", b"s" * 31),
    ),
)
def test_source_parent_rejects_invalid_exact_fields(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(_valid_source(), **{field: value})


@pytest.mark.parametrize(
    "bad_time",
    (
        _OBSERVED_AT.replace(tzinfo=None),
        _OBSERVED_AT.astimezone(timezone(timedelta(hours=9))),
        _OBSERVED_AT.replace(microsecond=1),
    ),
)
def test_scalar_evidence_requires_whole_second_exact_utc(
    bad_time: datetime,
) -> None:
    with pytest.raises(ValueError):
        TossHlitKrxMarketSafetyEvidence(
            symbol="005930",
            observed_at=bad_time,
            has_active_krx_vi=False,
            is_single_price_auction=False,
        )
