from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.shared.errors import FloatRejectedError
from autotrader.strategies.david_v6.models import (
    EvidenceState,
    MatchedIndicator,
    SetupGrade,
    StrategyFamily,
    V6Decision,
    V6Market,
)

NOW = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)


def matched(
    key: str = "MIG_REVERSAL",
    *,
    mandatory: bool = True,
    evidence_hash: bytes = b"m" * 32,
) -> MatchedIndicator:
    return MatchedIndicator(
        key=key,
        mandatory=mandatory,
        evidence_state=EvidenceState.AVAILABLE,
        evidence_hash=evidence_hash,
    )


def decision(**changes: object) -> V6Decision:
    values: dict[str, object] = {
        "id": UUID("019d0000-0000-7000-8000-000000000001"),
        "strategy_version_id": UUID("019d0000-0000-7000-8000-000000000002"),
        "setup_id": UUID("019d0000-0000-7000-8000-000000000003"),
        "feature_snapshot_id": UUID("019d0000-0000-7000-8000-000000000004"),
        "instrument_id": UUID("019d0000-0000-7000-8000-000000000005"),
        "market": V6Market.BINANCE_USDM,
        "family": StrategyFamily.HLIT,
        "grade": SetupGrade.A_CANDIDATE,
        "side": Side.BUY,
        "order_style": OrderStyle.LIMIT,
        "matched_indicators": (matched(),),
        "blockers": (),
        "planned_entry": Decimal("100"),
        "structural_stop": Decimal("99"),
        "target_price": Decimal("103"),
        "risk_fraction": Decimal("0.0025"),
        "calculated_quantity": Decimal("0.100"),
        "expected_cost": Decimal("0.25"),
        "source_evidence_hashes": (b"a" * 32, b"s" * 32),
        "exhaustion_timeframe": "30s",
        "completed_evidence_at": NOW - timedelta(seconds=1),
        "generated_at": NOW,
        "valid_until": NOW + timedelta(minutes=1),
    }
    values.update(changes)
    return V6Decision(**values)  # type: ignore[arg-type]


def rejected_decision(**changes: object) -> V6Decision:
    values: dict[str, object] = {
        "grade": SetupGrade.REJECT,
        "matched_indicators": (),
        "blockers": ("MISSING_EVIDENCE",),
        "planned_entry": None,
        "structural_stop": None,
        "target_price": None,
        "risk_fraction": Decimal("0"),
        "calculated_quantity": Decimal("0"),
        "expected_cost": None,
    }
    values.update(changes)
    return decision(**values)


def test_matched_indicator_is_one_available_technical_confirmation() -> None:
    indicator = matched()

    assert indicator.key == "MIG_REVERSAL"
    assert indicator.mandatory is True
    assert indicator.evidence_state is EvidenceState.AVAILABLE


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"key": ""}, "key"),
        ({"evidence_state": EvidenceState.STALE}, "AVAILABLE"),
        ({"evidence_hash": b"x" * 31}, "SHA-256"),
        ({"evidence_hash": bytearray(b"x" * 32)}, "SHA-256"),
    ],
)
def test_matched_indicator_rejects_noncanonical_evidence(
    changes: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "key": "MIG_REVERSAL",
        "mandatory": True,
        "evidence_state": EvidenceState.AVAILABLE,
        "evidence_hash": b"m" * 32,
    }
    values.update(changes)

    with pytest.raises((TypeError, ValueError), match=message):
        MatchedIndicator(**values)  # type: ignore[arg-type]


def test_decision_hash_binds_indicator_and_source_evidence() -> None:
    original = decision()
    changed_indicator = replace(
        original,
        matched_indicators=(matched("PROFILE", mandatory=False),),
    )
    changed_source = replace(
        original,
        source_evidence_hashes=(b"a" * 32, b"t" * 32),
        exhaustion_timeframe="30s",
    )

    assert original.decision_hash() != changed_indicator.decision_hash()
    assert original.decision_hash() != changed_source.decision_hash()


def test_decision_hash_canonicalizes_equivalent_decimal_scales() -> None:
    original = decision(
        planned_entry=Decimal("100.0"),
        calculated_quantity=Decimal("0.100"),
    )
    equivalent = replace(
        original,
        planned_entry=Decimal("100.00"),
        calculated_quantity=Decimal("0.1"),
    )

    assert original.decision_hash() == equivalent.decision_hash()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {
                "matched_indicators": (
                    matched("PROFILE", mandatory=False),
                    matched("MIG_REVERSAL"),
                )
            },
            "sorted",
        ),
        (
            {"matched_indicators": (matched(), matched())},
            "unique",
        ),
        (
            {"blockers": ("Z_BLOCKER", "A_BLOCKER")},
            "sorted",
        ),
        (
            {"source_evidence_hashes": (b"s" * 32, b"a" * 32)},
            "sorted",
        ),
        (
            {"source_evidence_hashes": (b"a" * 32, b"a" * 32)},
            "unique",
        ),
    ],
)
def test_decision_rejects_noncanonical_manifests(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        decision(**changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"planned_entry": Decimal("0")}, "positive"),
        ({"structural_stop": Decimal("100")}, "BUY"),
        ({"target_price": Decimal("100")}, "BUY"),
        ({"risk_fraction": Decimal("0")}, "positive"),
        ({"risk_fraction": Decimal("0.0075001")}, "ceiling"),
        ({"calculated_quantity": Decimal("0")}, "quantity"),
        ({"expected_cost": Decimal("-0.01")}, "cost"),
    ],
)
def test_tradeable_decision_rejects_unsafe_terms(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        decision(**changes)


def test_sell_decision_requires_target_below_entry_and_stop_above() -> None:
    sell = decision(
        side=Side.SELL,
        planned_entry=Decimal("100"),
        structural_stop=Decimal("101"),
        target_price=Decimal("97"),
    )

    assert sell.side is Side.SELL


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"blockers": ()}, "blocker"),
        ({"risk_fraction": Decimal("0.001")}, "zero risk"),
        ({"calculated_quantity": Decimal("1")}, "zero quantity"),
        ({"planned_entry": Decimal("100")}, "order terms"),
        ({"expected_cost": Decimal("0")}, "order terms"),
    ],
)
def test_rejected_decision_cannot_authorize_order_terms(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        rejected_decision(**changes)


def test_rejected_decision_is_persistable_without_trade_terms() -> None:
    rejected = rejected_decision()

    assert rejected.grade is SetupGrade.REJECT
    assert rejected.blockers == ("MISSING_EVIDENCE",)
    assert rejected.planned_entry is None
    assert rejected.calculated_quantity == 0


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"completed_evidence_at": NOW + timedelta(seconds=1)}, "completed evidence"),
        ({"generated_at": datetime(2026, 8, 24)}, "UTC"),
        ({"valid_until": NOW}, "valid_until"),
    ],
)
def test_decision_rejects_invalid_timeline(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        decision(**changes)


def test_decision_rejects_float_at_numeric_boundary() -> None:
    with pytest.raises(FloatRejectedError, match="float"):
        decision(risk_fraction=0.0025)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"id": UUID("12345678-1234-4234-8234-123456789abc")}, "UUIDv7"),
        ({"market": "KRX_CASH"}, "market"),
        ({"family": "HLIT"}, "family"),
        ({"grade": "NORMAL"}, "grade"),
        ({"side": "BUY"}, "side"),
        ({"order_style": "LIMIT"}, "order_style"),
    ],
)
def test_decision_rejects_untyped_identity_and_enum_values(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        decision(**changes)


def test_metodo_family_is_rejected_for_binance_futures() -> None:
    with pytest.raises(ValueError, match="METODO"):
        decision(family=StrategyFamily.METODO, market=V6Market.BINANCE_USDM)
