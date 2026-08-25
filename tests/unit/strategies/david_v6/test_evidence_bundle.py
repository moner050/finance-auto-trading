from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.strategies.david_v6 import V6EvidenceBundle
from autotrader.strategies.david_v6.evidence import (
    EvidenceItem,
    EvidenceProvenance,
)
from autotrader.strategies.david_v6.models import EvidenceState, V6Market

NOW = datetime(2026, 8, 24, 1, tzinfo=UTC)
INSTRUMENT_ID = UUID("019d0000-0000-7000-8000-000000001001")
DIGEST = "a" * 64


def _provenance(**changes: object) -> EvidenceProvenance:
    values: dict[str, object] = {
        "source": "KIS",
        "source_key": "005930:1m:2026-08-24T01:00:00Z",
        "source_timezone": "Asia/Seoul",
        "observed_at": NOW,
        "captured_at": NOW,
        "digest_sha256": DIGEST,
    }
    values.update(changes)
    return EvidenceProvenance(**values)  # type: ignore[arg-type]


def _bar() -> CompletedOhlcvBar:
    return CompletedOhlcvBar(
        timestamp=NOW,
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=Decimal("1"),
    )


def _missing() -> EvidenceItem[object]:
    return EvidenceItem(
        state=EvidenceState.UNKNOWN,
        value=None,
        provenance=None,
        blocker_code="EVIDENCE_NOT_EVALUATED",
    )


def _bundle(
    *,
    market: V6Market = V6Market.KRX_CASH,
    bars: dict[str, EvidenceItem[tuple[CompletedOhlcvBar, ...]]] | None = None,
    decision_at: datetime = NOW,
) -> V6EvidenceBundle:
    missing = _missing()
    return V6EvidenceBundle(
        market=market,
        instrument_id=INSTRUMENT_ID,
        decision_at=decision_at,
        bars={} if bars is None else bars,
        universe=missing,
        regime=missing,
        metodo=missing,
        zones=missing,
        divergence=missing,
        exhaustion=missing,
        order_flow=missing,
        profile=missing,
        calendar=missing,
        session=missing,
        costs=missing,
    )


def test_available_evidence_requires_value_and_provenance_together() -> None:
    with pytest.raises(ValueError, match="provenance"):
        EvidenceItem(
            state=EvidenceState.AVAILABLE,
            value="fact",
            provenance=None,
            blocker_code=None,
        )

    with pytest.raises(ValueError, match="without a value"):
        EvidenceItem[object](
            state=EvidenceState.UNKNOWN,
            value=None,
            provenance=_provenance(),
            blocker_code="FUTURE_OBSERVATION",
        )


def test_provenance_rejects_non_sha256_and_naive_instants() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        _provenance(digest_sha256="abc")

    with pytest.raises(ValueError, match="timezone-aware"):
        _provenance(observed_at=datetime(2026, 8, 24, 1))


def test_cash_bundle_rejects_30_second_evidence() -> None:
    bars = {
        "30s": EvidenceItem(
            state=EvidenceState.AVAILABLE,
            value=(_bar(),),
            provenance=_provenance(),
            blocker_code=None,
        )
    }

    with pytest.raises(ValueError, match=r"cash.*30-second"):
        _bundle(bars=bars)


def test_bundle_rejects_timezone_naive_decision_instant() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _bundle(decision_at=datetime(2026, 8, 24, 1))


def test_bundle_copies_bar_mapping_into_an_immutable_snapshot() -> None:
    source: dict[str, EvidenceItem[tuple[CompletedOhlcvBar, ...]]] = {}
    bundle = _bundle(market=V6Market.BINANCE_USDM, bars=source)

    source["30s"] = EvidenceItem(
        state=EvidenceState.AVAILABLE,
        value=(_bar(),),
        provenance=_provenance(source="BINANCE", source_timezone="UTC"),
        blocker_code=None,
    )

    assert dict(bundle.bars) == {}
    with pytest.raises(TypeError):
        bundle.bars["30s"] = source["30s"]  # type: ignore[index]
