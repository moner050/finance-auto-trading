from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

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
from autotrader.strategies.david_v6.manifest import (
    STRATEGY_CODE,
    STRATEGY_VERSION,
    V6Manifest,
)
from autotrader.strategies.david_v6.models import (
    SetupGrade,
    V6Decision,
    canonical_v6_hash,
)

_SIGNAL_TYPE = "DAVID_V6_ENTRY"


@dataclass(frozen=True, slots=True)
class PersistedV6Decision:
    id: UUID
    manifest_id: UUID
    strategy_signal_id: UUID | None
    decision_hash: bytes


class DavidV6Repository:
    """Persists provider-neutral v6 governance in the caller transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def persist_manifest(self, manifest: V6Manifest) -> V6Manifest:
        if type(manifest) is not V6Manifest:
            raise ValueError("exact V6Manifest is required")
        manifest.__post_init__()
        version = await self._session.scalar(
            select(StrategyVersion)
            .where(StrategyVersion.id == manifest.strategy_version_id)
            .with_for_update()
        )
        definition = None
        if isinstance(version, StrategyVersion):
            definition = await self._session.scalar(
                select(StrategyDefinition)
                .where(StrategyDefinition.id == version.definition_id)
                .with_for_update()
            )
        _require_manifest_authority(manifest, version, definition)

        existing_by_id = await self._session.scalar(
            select(DavidV6ManifestRow)
            .where(DavidV6ManifestRow.id == manifest.id)
            .with_for_update()
        )
        if existing_by_id is not None:
            _require_matching_manifest(existing_by_id, manifest)
            return manifest
        existing = await self._session.scalar(
            select(DavidV6ManifestRow)
            .where(
                DavidV6ManifestRow.strategy_version_id == manifest.strategy_version_id,
                DavidV6ManifestRow.source_sha256 == manifest.source_sha256,
                DavidV6ManifestRow.configuration_hash == manifest.configuration_hash,
            )
            .with_for_update()
        )
        if existing is not None:
            _require_matching_manifest(existing, manifest)
            return manifest
        self._session.add(
            DavidV6ManifestRow(
                id=manifest.id,
                strategy_version_id=manifest.strategy_version_id,
                strategy_code=STRATEGY_CODE,
                strategy_version=STRATEGY_VERSION,
                source_sha256=manifest.source_sha256,
                design_sha256=manifest.design_sha256,
                configuration_hash=manifest.configuration_hash,
                registered_at=manifest.registered_at,
            )
        )
        await self._session.flush()
        return manifest

    async def persist_decision(self, decision: V6Decision) -> PersistedV6Decision:
        if type(decision) is not V6Decision:
            raise ValueError("exact V6Decision is required")
        decision.__post_init__()
        manifest = await self._session.scalar(
            select(DavidV6ManifestRow)
            .where(
                DavidV6ManifestRow.strategy_version_id == decision.strategy_version_id,
                DavidV6ManifestRow.strategy_code == STRATEGY_CODE,
                DavidV6ManifestRow.strategy_version == STRATEGY_VERSION,
            )
            .with_for_update()
        )
        setup = await self._session.scalar(
            select(StrategySetup)
            .where(StrategySetup.id == decision.setup_id)
            .with_for_update()
        )
        snapshot = await self._session.scalar(
            select(StrategyFeatureSnapshot)
            .where(StrategyFeatureSnapshot.id == decision.feature_snapshot_id)
            .with_for_update()
        )
        schema = None
        if isinstance(snapshot, StrategyFeatureSnapshot):
            schema = await self._session.scalar(
                select(StrategyFeatureSchema)
                .where(StrategyFeatureSchema.id == snapshot.feature_schema_id)
                .with_for_update()
            )
        instrument = await self._session.scalar(
            select(CoreInstrument)
            .where(CoreInstrument.id == decision.instrument_id)
            .with_for_update()
        )
        _require_decision_authority(
            decision,
            manifest=manifest,
            setup=setup,
            snapshot=snapshot,
            schema=schema,
            instrument=instrument,
        )
        assert isinstance(manifest, DavidV6ManifestRow)

        digest = decision.decision_hash()
        existing = await self._session.scalar(
            select(DavidV6DecisionRow)
            .where(
                or_(
                    DavidV6DecisionRow.id == decision.id,
                    (
                        (DavidV6DecisionRow.setup_id == decision.setup_id)
                        & (DavidV6DecisionRow.instrument_id == decision.instrument_id)
                        & (DavidV6DecisionRow.decision_hash == digest)
                    ),
                )
            )
            .with_for_update()
        )
        if existing is not None:
            await self._require_matching_decision(
                existing=existing,
                manifest=manifest,
                decision=decision,
                digest=digest,
            )
            return _persisted(existing)

        occupied_signal = await self._session.scalar(
            select(StrategySignal.id)
            .where(StrategySignal.id == decision.id)
            .with_for_update()
        )
        if occupied_signal is not None:
            raise ValueError("v6 decision signal identity is already occupied")
        signal_id = None
        if decision.grade is not SetupGrade.REJECT:
            signal_id = decision.id
            assert decision.planned_entry is not None
            assert decision.structural_stop is not None
            self._session.add(
                StrategySignal(
                    id=signal_id,
                    strategy_version_id=decision.strategy_version_id,
                    setup_id=decision.setup_id,
                    feature_snapshot_id=decision.feature_snapshot_id,
                    instrument_id=decision.instrument_id,
                    signal_type=_SIGNAL_TYPE,
                    side=decision.side.value,
                    order_style=decision.order_style.value,
                    planned_entry_price=decision.planned_entry,
                    trigger_price=decision.planned_entry,
                    invalidation_price=decision.structural_stop,
                    generated_at=decision.generated_at,
                    valid_until=decision.valid_until,
                    session_type=decision.market.value,
                    signal_hash=digest,
                )
            )
            # The decision row points at this signal and the models carry no
            # relationship, so the unit of work cannot order the two inserts.
            await self._session.flush()

        row = DavidV6DecisionRow(
            id=decision.id,
            manifest_id=manifest.id,
            strategy_signal_id=signal_id,
            strategy_version_id=decision.strategy_version_id,
            setup_id=decision.setup_id,
            feature_snapshot_id=decision.feature_snapshot_id,
            instrument_id=decision.instrument_id,
            market=decision.market.value,
            family=decision.family.value,
            grade=decision.grade.value,
            side=decision.side.value,
            order_style=decision.order_style.value,
            matched_indicator_count=len(decision.matched_indicators),
            blocker_count=len(decision.blockers),
            planned_entry=decision.planned_entry,
            structural_stop=decision.structural_stop,
            target_price=decision.target_price,
            risk_fraction=decision.risk_fraction,
            calculated_quantity=decision.calculated_quantity,
            expected_cost=decision.expected_cost,
            source_evidence_hashes=[
                evidence.hex() for evidence in decision.source_evidence_hashes
            ],
            source_evidence_manifest_hash=canonical_v6_hash(
                decision.source_evidence_hashes
            ),
            completed_evidence_at=decision.completed_evidence_at,
            generated_at=decision.generated_at,
            valid_until=decision.valid_until,
            decision_hash=digest,
        )
        self._session.add(row)
        for ordinal, indicator in enumerate(decision.matched_indicators):
            self._session.add(
                DavidV6IndicatorRow(
                    decision_id=decision.id,
                    ordinal=ordinal,
                    indicator_key=indicator.key,
                    mandatory=indicator.mandatory,
                    evidence_state=indicator.evidence_state.value,
                    evidence_hash=indicator.evidence_hash,
                )
            )
        for ordinal, blocker in enumerate(decision.blockers):
            self._session.add(
                DavidV6BlockerRow(
                    decision_id=decision.id,
                    ordinal=ordinal,
                    blocker_code=blocker,
                )
            )
        await self._session.flush()
        return _persisted(row)

    async def indicator_keys(self, decision_id: UUID) -> tuple[str, ...]:
        _require_uuid7(decision_id, "decision_id")
        return tuple(
            await self._session.scalars(
                select(DavidV6IndicatorRow.indicator_key)
                .where(DavidV6IndicatorRow.decision_id == decision_id)
                .order_by(DavidV6IndicatorRow.ordinal)
            )
        )

    async def blocker_codes(self, decision_id: UUID) -> tuple[str, ...]:
        _require_uuid7(decision_id, "decision_id")
        return tuple(
            await self._session.scalars(
                select(DavidV6BlockerRow.blocker_code)
                .where(DavidV6BlockerRow.decision_id == decision_id)
                .order_by(DavidV6BlockerRow.ordinal)
            )
        )

    async def _require_matching_decision(
        self,
        *,
        existing: DavidV6DecisionRow,
        manifest: DavidV6ManifestRow,
        decision: V6Decision,
        digest: bytes,
    ) -> None:
        _require_matching_decision_row(existing, manifest, decision, digest)
        indicators = tuple(
            await self._session.scalars(
                select(DavidV6IndicatorRow)
                .where(DavidV6IndicatorRow.decision_id == existing.id)
                .order_by(DavidV6IndicatorRow.ordinal)
                .with_for_update()
            )
        )
        blockers = tuple(
            await self._session.scalars(
                select(DavidV6BlockerRow)
                .where(DavidV6BlockerRow.decision_id == existing.id)
                .order_by(DavidV6BlockerRow.ordinal)
                .with_for_update()
            )
        )
        if len(indicators) != len(decision.matched_indicators):
            raise ValueError("v6 indicator manifest count mismatch")
        for ordinal, (stored, expected) in enumerate(
            zip(indicators, decision.matched_indicators, strict=True)
        ):
            if (
                stored.ordinal != ordinal
                or stored.indicator_key != expected.key
                or stored.mandatory != expected.mandatory
                or stored.evidence_state != expected.evidence_state.value
                or stored.evidence_hash != expected.evidence_hash
            ):
                raise ValueError("v6 indicator manifest mismatch")
        if len(blockers) != len(decision.blockers):
            raise ValueError("v6 blocker manifest count mismatch")
        for ordinal, (stored, expected) in enumerate(
            zip(blockers, decision.blockers, strict=True)
        ):
            if stored.ordinal != ordinal or stored.blocker_code != expected:
                raise ValueError("v6 blocker manifest mismatch")
        signal = await self._session.scalar(
            select(StrategySignal)
            .where(StrategySignal.id == existing.strategy_signal_id)
            .with_for_update()
        )
        _require_matching_signal(signal, decision, digest)


def _require_manifest_authority(
    manifest: V6Manifest,
    version: object | None,
    definition: object | None,
) -> None:
    if not isinstance(version, StrategyVersion) or (
        version.id != manifest.strategy_version_id
        or version.version != STRATEGY_VERSION
        or version.research_only
        or version.status not in {"SHADOW", "LIVE_APPROVED"}
    ):
        raise ValueError("exact executable v6 strategy version is required")
    if not isinstance(definition, StrategyDefinition) or (
        definition.id != version.definition_id
        or definition.code != STRATEGY_CODE
        or definition.research_only
        or definition.configuration_hash != manifest.configuration_hash
    ):
        raise ValueError("exact executable v6 strategy definition is required")


def _require_decision_authority(
    decision: V6Decision,
    *,
    manifest: object | None,
    setup: object | None,
    snapshot: object | None,
    schema: object | None,
    instrument: object | None,
) -> None:
    if not isinstance(manifest, DavidV6ManifestRow) or (
        manifest.strategy_version_id != decision.strategy_version_id
        or manifest.strategy_code != STRATEGY_CODE
        or manifest.strategy_version != STRATEGY_VERSION
        or manifest.registered_at > decision.generated_at
    ):
        raise ValueError("exact persisted v6 manifest is required")
    if not isinstance(setup, StrategySetup) or (
        setup.id != decision.setup_id
        or setup.strategy_version_id != decision.strategy_version_id
    ):
        raise ValueError("decision setup provenance mismatch")
    if not isinstance(snapshot, StrategyFeatureSnapshot) or (
        snapshot.id != decision.feature_snapshot_id
        or snapshot.available_at > decision.completed_evidence_at
    ):
        raise ValueError("completed feature snapshot is required")
    if not isinstance(schema, StrategyFeatureSchema) or (
        schema.id != snapshot.feature_schema_id
        or schema.strategy_version_id != decision.strategy_version_id
    ):
        raise ValueError("feature schema provenance mismatch")
    if not isinstance(instrument, CoreInstrument) or (
        instrument.id != decision.instrument_id or instrument.status != "ACTIVE"
    ):
        raise ValueError("active canonical instrument is required")


def _require_matching_manifest(
    stored: DavidV6ManifestRow, manifest: V6Manifest
) -> None:
    if (
        stored.id != manifest.id
        or stored.strategy_version_id != manifest.strategy_version_id
        or stored.strategy_code != STRATEGY_CODE
        or stored.strategy_version != STRATEGY_VERSION
        or stored.source_sha256 != manifest.source_sha256
        or stored.design_sha256 != manifest.design_sha256
        or stored.configuration_hash != manifest.configuration_hash
        or stored.registered_at != manifest.registered_at
    ):
        raise ValueError("v6 manifest evidence mismatch")


def _require_matching_decision_row(
    stored: DavidV6DecisionRow,
    manifest: DavidV6ManifestRow,
    decision: V6Decision,
    digest: bytes,
) -> None:
    signal_id = None if decision.grade is SetupGrade.REJECT else decision.id
    expected = (
        decision.id,
        manifest.id,
        signal_id,
        decision.strategy_version_id,
        decision.setup_id,
        decision.feature_snapshot_id,
        decision.instrument_id,
        decision.market.value,
        decision.family.value,
        decision.grade.value,
        decision.side.value,
        decision.order_style.value,
        len(decision.matched_indicators),
        len(decision.blockers),
        decision.planned_entry,
        decision.structural_stop,
        decision.target_price,
        decision.risk_fraction,
        decision.calculated_quantity,
        decision.expected_cost,
        [evidence.hex() for evidence in decision.source_evidence_hashes],
        canonical_v6_hash(decision.source_evidence_hashes),
        decision.completed_evidence_at,
        decision.generated_at,
        decision.valid_until,
        digest,
    )
    fields = (
        "id",
        "manifest_id",
        "strategy_signal_id",
        "strategy_version_id",
        "setup_id",
        "feature_snapshot_id",
        "instrument_id",
        "market",
        "family",
        "grade",
        "side",
        "order_style",
        "matched_indicator_count",
        "blocker_count",
        "planned_entry",
        "structural_stop",
        "target_price",
        "risk_fraction",
        "calculated_quantity",
        "expected_cost",
        "source_evidence_hashes",
        "source_evidence_manifest_hash",
        "completed_evidence_at",
        "generated_at",
        "valid_until",
        "decision_hash",
    )
    if tuple(getattr(stored, field) for field in fields) != expected:
        raise ValueError("v6 decision evidence mismatch")


def _require_matching_signal(
    stored: object | None, decision: V6Decision, digest: bytes
) -> None:
    if decision.grade is SetupGrade.REJECT:
        if stored is not None:
            raise ValueError("REJECT decision cannot have a strategy signal")
        return
    if not isinstance(stored, StrategySignal) or (
        stored.id != decision.id
        or stored.strategy_version_id != decision.strategy_version_id
        or stored.setup_id != decision.setup_id
        or stored.feature_snapshot_id != decision.feature_snapshot_id
        or stored.instrument_id != decision.instrument_id
        or stored.signal_type != _SIGNAL_TYPE
        or stored.side != decision.side.value
        or stored.order_style != decision.order_style.value
        or stored.planned_entry_price != decision.planned_entry
        or stored.trigger_price != decision.planned_entry
        or stored.invalidation_price != decision.structural_stop
        or stored.generated_at != decision.generated_at
        or stored.valid_until != decision.valid_until
        or stored.session_type != decision.market.value
        or stored.signal_hash != digest
    ):
        raise ValueError("v6 strategy signal evidence mismatch")


def _persisted(row: DavidV6DecisionRow) -> PersistedV6Decision:
    return PersistedV6Decision(
        id=row.id,
        manifest_id=row.manifest_id,
        strategy_signal_id=row.strategy_signal_id,
        decision_hash=row.decision_hash,
    )


def _require_uuid7(value: object, name: str) -> None:
    if not isinstance(value, UUID) or value.version != 7:
        raise ValueError(f"{name} must be UUIDv7")
