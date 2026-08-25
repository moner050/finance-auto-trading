from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.domain.enums import OrderStyle, Side
from autotrader.persistence.mysql.models.core import CoreInstrument
from autotrader.persistence.mysql.models.david_v6 import (
    DavidV6BlockerRow,
    DavidV6DecisionRow,
    DavidV6IndicatorRow,
    DavidV6ManifestRow,
)
from autotrader.persistence.mysql.models.strategy import (
    StrategyDefinition,
    StrategyFeatureSchema,
    StrategyFeatureSnapshot,
    StrategySetup,
    StrategySignal,
    StrategyVersion,
)
from autotrader.persistence.mysql.repositories.david_v6 import DavidV6Repository
from autotrader.shared.ids import new_uuid7
from autotrader.strategies.david_v6.manifest import (
    V6_DESIGN_SHA256,
    V6_SOURCE_SHA256,
    V6Manifest,
    v6_configuration_hash,
)
from autotrader.strategies.david_v6.models import (
    EvidenceState,
    MatchedIndicator,
    SetupGrade,
    StrategyFamily,
    V6Decision,
    V6Market,
)

NOW = datetime(2026, 8, 24, 2, 0, tzinfo=UTC)


class FakeScalarResult:
    def __init__(self, values: tuple[object, ...]) -> None:
        self._values = values

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._values)


class FakeSession:
    def __init__(
        self,
        scalar_responses: tuple[object | None, ...],
        scalars_responses: tuple[tuple[object, ...], ...] = (),
    ) -> None:
        self.scalar_responses = list(scalar_responses)
        self.scalars_responses = list(scalars_responses)
        self.added: list[object] = []
        self.flush_count = 0

    async def scalar(self, statement: object) -> object | None:
        del statement
        return self.scalar_responses.pop(0)

    async def scalars(self, statement: object) -> FakeScalarResult:
        del statement
        return FakeScalarResult(self.scalars_responses.pop(0))

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flush_count += 1


def _manifest() -> V6Manifest:
    return V6Manifest(
        id=new_uuid7(),
        strategy_version_id=new_uuid7(),
        source_sha256=V6_SOURCE_SHA256,
        design_sha256=V6_DESIGN_SHA256,
        configuration_hash=v6_configuration_hash(),
        registered_at=NOW,
    )


def _decision(*, strategy_version_id: UUID | None = None) -> V6Decision:
    return V6Decision(
        id=new_uuid7(),
        strategy_version_id=strategy_version_id or new_uuid7(),
        setup_id=new_uuid7(),
        feature_snapshot_id=new_uuid7(),
        instrument_id=new_uuid7(),
        market=V6Market.KRX_CASH,
        family=StrategyFamily.HLIT,
        grade=SetupGrade.NORMAL,
        side=Side.BUY,
        order_style=OrderStyle.LIMIT,
        matched_indicators=(
            MatchedIndicator(
                key="MIG_REVERSAL",
                mandatory=True,
                evidence_state=EvidenceState.AVAILABLE,
                evidence_hash=b"i" * 32,
            ),
        ),
        blockers=(),
        planned_entry=Decimal("70000"),
        structural_stop=Decimal("68000"),
        target_price=Decimal("74000"),
        risk_fraction=Decimal("0.0015"),
        calculated_quantity=Decimal("10"),
        expected_cost=Decimal("1000"),
        source_evidence_hashes=(b"e" * 32,),
        completed_evidence_at=NOW,
        generated_at=NOW + timedelta(seconds=1),
        valid_until=NOW + timedelta(minutes=5),
    )


def _strategy_authority(
    manifest: V6Manifest,
) -> tuple[StrategyVersion, StrategyDefinition]:
    definition_id = new_uuid7()
    return (
        StrategyVersion(
            id=manifest.strategy_version_id,
            definition_id=definition_id,
            version="v6.0-op-20260824.1",
            status="SHADOW",
            research_only=False,
        ),
        StrategyDefinition(
            id=definition_id,
            code="DAVID_TRULLAS_V6",
            research_only=False,
            configuration_hash=manifest.configuration_hash,
        ),
    )


def _manifest_row(manifest: V6Manifest) -> DavidV6ManifestRow:
    return DavidV6ManifestRow(
        id=manifest.id,
        strategy_version_id=manifest.strategy_version_id,
        strategy_code="DAVID_TRULLAS_V6",
        strategy_version="v6.0-op-20260824.1",
        source_sha256=manifest.source_sha256,
        design_sha256=manifest.design_sha256,
        configuration_hash=manifest.configuration_hash,
        registered_at=manifest.registered_at,
    )


def _decision_authority(
    manifest: V6Manifest, decision: V6Decision
) -> tuple[object, ...]:
    feature_schema_id = new_uuid7()
    return (
        _manifest_row(manifest),
        StrategySetup(
            id=decision.setup_id,
            strategy_version_id=decision.strategy_version_id,
            status="DETECTED",
        ),
        StrategyFeatureSnapshot(
            id=decision.feature_snapshot_id,
            feature_schema_id=feature_schema_id,
            payload_hash=b"p" * 32,
            available_at=decision.completed_evidence_at,
        ),
        StrategyFeatureSchema(
            id=feature_schema_id,
            strategy_version_id=decision.strategy_version_id,
            schema_hash=b"f" * 32,
        ),
        CoreInstrument(
            id=decision.instrument_id,
            exchange_id=new_uuid7(),
            code="005930",
            name="Samsung Electronics",
            instrument_type="EQUITY",
            status="ACTIVE",
            created_at=NOW,
        ),
    )


def test_v6_manifest_rejects_any_noncanonical_authority_hash() -> None:
    with pytest.raises(ValueError, match="source_sha256"):
        replace(_manifest(), source_sha256=b"x" * 32)


def test_repository_interface_exposes_manifest_decision_and_child_queries() -> None:
    assert hasattr(DavidV6Repository, "persist_manifest")
    assert hasattr(DavidV6Repository, "persist_decision")
    assert hasattr(DavidV6Repository, "indicator_keys")
    assert hasattr(DavidV6Repository, "blocker_codes")


def test_tradeable_decision_fixture_binds_exact_manifest_version() -> None:
    manifest = _manifest()
    decision = _decision(strategy_version_id=manifest.strategy_version_id)

    assert decision.strategy_version_id == manifest.strategy_version_id
    assert decision.decision_hash() == decision.decision_hash()


@pytest.mark.asyncio
async def test_persist_manifest_is_exact_and_idempotent() -> None:
    manifest = _manifest()
    version, definition = _strategy_authority(manifest)
    session = FakeSession((version, definition, None, None))
    repository = DavidV6Repository(cast(AsyncSession, session))

    result = await repository.persist_manifest(manifest)

    assert result is manifest
    assert len(session.added) == 1
    assert isinstance(session.added[0], DavidV6ManifestRow)
    assert session.flush_count == 1

    retry = FakeSession((version, definition, session.added[0]))
    retried = await DavidV6Repository(cast(AsyncSession, retry)).persist_manifest(
        manifest
    )
    assert retried is manifest
    assert retry.added == []
    assert retry.flush_count == 0


@pytest.mark.asyncio
async def test_tradeable_decision_persists_signal_indicator_and_decision_once() -> None:
    manifest = _manifest()
    decision = _decision(strategy_version_id=manifest.strategy_version_id)
    session = FakeSession((*_decision_authority(manifest, decision), None, None))
    repository = DavidV6Repository(cast(AsyncSession, session))

    persisted = await repository.persist_decision(decision)

    assert persisted.id == decision.id
    assert persisted.strategy_signal_id == decision.id
    assert persisted.decision_hash == decision.decision_hash()
    assert [type(value) for value in session.added] == [
        StrategySignal,
        DavidV6DecisionRow,
        DavidV6IndicatorRow,
    ]
    assert session.flush_count == 1


@pytest.mark.asyncio
async def test_reject_decision_persists_no_generic_strategy_signal() -> None:
    manifest = _manifest()
    tradeable = _decision(strategy_version_id=manifest.strategy_version_id)
    decision = replace(
        tradeable,
        grade=SetupGrade.REJECT,
        blockers=("MISSING_EVIDENCE",),
        planned_entry=None,
        structural_stop=None,
        target_price=None,
        risk_fraction=Decimal("0"),
        calculated_quantity=Decimal("0"),
        expected_cost=None,
    )
    session = FakeSession((*_decision_authority(manifest, decision), None, None))

    persisted = await DavidV6Repository(cast(AsyncSession, session)).persist_decision(
        decision
    )

    assert persisted.strategy_signal_id is None
    assert [type(value) for value in session.added] == [
        DavidV6DecisionRow,
        DavidV6IndicatorRow,
        DavidV6BlockerRow,
    ]
    assert session.flush_count == 1


@pytest.mark.asyncio
async def test_exact_decision_retry_returns_existing_without_writes() -> None:
    manifest = _manifest()
    decision = _decision(strategy_version_id=manifest.strategy_version_id)
    first = FakeSession((*_decision_authority(manifest, decision), None, None))
    await DavidV6Repository(cast(AsyncSession, first)).persist_decision(decision)
    signal, row, indicator = first.added
    retry = FakeSession(
        (*_decision_authority(manifest, decision), row, signal),
        ((indicator,), ()),
    )

    persisted = await DavidV6Repository(cast(AsyncSession, retry)).persist_decision(
        decision
    )

    assert persisted.id == decision.id
    assert retry.added == []
    assert retry.flush_count == 0


@pytest.mark.asyncio
async def test_child_manifest_queries_preserve_provider_neutral_order() -> None:
    decision_id = new_uuid7()
    session = FakeSession((), (("MIG_REVERSAL", "PROFILE"), ("STALE_BAR",)))
    repository = DavidV6Repository(cast(AsyncSession, session))

    assert await repository.indicator_keys(decision_id) == (
        "MIG_REVERSAL",
        "PROFILE",
    )
    assert await repository.blocker_codes(decision_id) == ("STALE_BAR",)
