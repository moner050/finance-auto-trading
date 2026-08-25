from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from autotrader.domain.krx_instrument_authority import (
    ActivatedKrxCommonStockAuthority,
    KrxAuthorityActivationManifest,
    KrxCashMarket,
    KrxCommonStockAuthoritySnapshot,
    KrxCommonStockInstrumentAuthority,
    prepare_krx_authority_activation,
)
from autotrader.shared.ids import new_uuid7

SOURCE_TIME = datetime(2026, 8, 19, 1, 0, tzinfo=UTC)
ACTIVATED_AT = datetime(2026, 8, 19, 3, 0, tzinfo=UTC)
TRADING_DATE = date(2026, 8, 19)


def _snapshot(
    *, captured_at: datetime = SOURCE_TIME
) -> KrxCommonStockAuthoritySnapshot:
    return KrxCommonStockAuthoritySnapshot.build(
        captured_at=captured_at,
        kospi_source_hash=b"k" * 32,
        kosdaq_source_hash=b"q" * 32,
        instruments=(
            KrxCommonStockInstrumentAuthority(
                market=KrxCashMarket.KOSPI,
                symbol="005930",
                standard_code="KR7005930003",
                name="삼성전자",
                security_group_code="ST",
                etp_product_class_code="0",
                preferred_stock_class_code="0",
            ),
        ),
    )


def _manifest() -> KrxAuthorityActivationManifest:
    return prepare_krx_authority_activation(
        snapshot_id=new_uuid7(),
        snapshot=_snapshot(),
        calendar_evidence_hash=b"c" * 32,
        requested_date=TRADING_DATE,
        activated_at=ACTIVATED_AT,
    )


def test_activation_is_bound_to_current_kst_date_and_next_midnight() -> None:
    manifest = _manifest()

    assert manifest.source_last_modified_at == SOURCE_TIME
    assert manifest.trading_date == TRADING_DATE
    assert manifest.valid_from == ACTIVATED_AT
    assert manifest.valid_until == datetime(2026, 8, 19, 15, 0, tzinfo=UTC)
    assert len(manifest.activation_hash) == 32


@pytest.mark.parametrize(
    ("snapshot", "requested_date", "activated_at"),
    [
        (_snapshot(), date(2026, 8, 20), ACTIVATED_AT),
        (
            _snapshot(captured_at=datetime(2026, 8, 18, 14, 59, 59, tzinfo=UTC)),
            TRADING_DATE,
            ACTIVATED_AT,
        ),
        (_snapshot(), TRADING_DATE, ACTIVATED_AT.replace(tzinfo=None)),
        (
            _snapshot(),
            TRADING_DATE,
            ACTIVATED_AT.astimezone(timezone(timedelta(hours=9))),
        ),
        (_snapshot(), TRADING_DATE, ACTIVATED_AT.replace(microsecond=1)),
        (_snapshot(), TRADING_DATE, SOURCE_TIME - timedelta(seconds=1)),
    ],
)
def test_activation_rejects_date_or_time_mismatch(
    snapshot: KrxCommonStockAuthoritySnapshot,
    requested_date: date,
    activated_at: datetime,
) -> None:
    with pytest.raises(ValueError):
        prepare_krx_authority_activation(
            snapshot_id=new_uuid7(),
            snapshot=snapshot,
            calendar_evidence_hash=b"c" * 32,
            requested_date=requested_date,
            activated_at=activated_at,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"calendar_evidence_hash": b"x" * 32},
        {"source_hash": b"x" * 32},
        {"valid_until": datetime(2026, 8, 19, 15, 0, 1, tzinfo=UTC)},
        {"activation_hash": b"x" * 32},
    ],
)
def test_public_manifest_recomputes_every_activation_binding(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        replace(_manifest(), **changes)


def test_public_activated_authority_revalidates_snapshot_and_manifest() -> None:
    snapshot = _snapshot()
    manifest = prepare_krx_authority_activation(
        snapshot_id=new_uuid7(),
        snapshot=snapshot,
        calendar_evidence_hash=b"c" * 32,
        requested_date=TRADING_DATE,
        activated_at=ACTIVATED_AT,
    )
    activated = ActivatedKrxCommonStockAuthority(
        snapshot=snapshot,
        activation_id=new_uuid7(),
        trading_date=manifest.trading_date,
        valid_from=manifest.valid_from,
        valid_until=manifest.valid_until,
        activation_hash=manifest.activation_hash,
    )

    object.__setattr__(snapshot.instruments[0], "name", "변조종목")
    with pytest.raises(ValueError, match="snapshot"):
        activated.__post_init__()
