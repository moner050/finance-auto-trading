from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.strategy import (
    StrategyDefinition,
    StrategyFeatureSchema,
    StrategyFeatureSnapshot,
    StrategyRule,
    StrategyRuleSource,
    StrategySetup,
    StrategySignal,
    StrategySourceReference,
    StrategyVersion,
)
from autotrader.strategies.common.decisions import StrategyDecision, StrategyStatus


class StrategyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def persist_decision(self, decision: StrategyDecision) -> StrategySignal:
        setup = await self._session.scalar(
            select(StrategySetup)
            .where(StrategySetup.id == decision.setup_id)
            .with_for_update()
        )
        feature_version = await self._session.scalar(
            select(StrategyFeatureSchema.strategy_version_id)
            .join(
                StrategyFeatureSnapshot,
                StrategyFeatureSnapshot.feature_schema_id == StrategyFeatureSchema.id,
            )
            .where(StrategyFeatureSnapshot.id == decision.feature_snapshot_id)
            .with_for_update()
        )
        if (
            setup is None
            or feature_version is None
            or setup.strategy_version_id != decision.strategy_version_id
            or feature_version != decision.strategy_version_id
        ):
            raise ValueError("decision provenance must use one strategy version")
        signal_hash = decision.decision_hash()
        existing = await self._session.scalar(
            select(StrategySignal).where(
                StrategySignal.setup_id == decision.setup_id,
                StrategySignal.signal_type == decision.intent_type,
            )
        )
        if existing is not None:
            if existing.signal_hash != signal_hash:
                raise ValueError("strategy signal identity payload mismatch")
            return existing
        signal = StrategySignal(
            id=decision.id,
            strategy_version_id=decision.strategy_version_id,
            setup_id=decision.setup_id,
            feature_snapshot_id=decision.feature_snapshot_id,
            instrument_id=decision.instrument_id,
            signal_type=decision.intent_type,
            side=decision.side,
            order_style=decision.order_style,
            planned_entry_price=decision.planned_entry,
            trigger_price=decision.trigger_price,
            invalidation_price=decision.invalidation_price,
            generated_at=decision.generated_at,
            valid_until=decision.valid_until,
            session_type=decision.session_type,
            signal_hash=signal_hash,
        )
        self._session.add(signal)
        await self._session.flush()
        return signal

    async def promote_live(self, strategy_version_id: object) -> None:
        version = await self._session.scalar(
            select(StrategyVersion)
            .where(StrategyVersion.id == strategy_version_id)
            .with_for_update()
        )
        if version is None:
            raise LookupError("strategy version not found")
        if version.status == StrategyStatus.RETIRED:
            raise ValueError("retired strategy version cannot be promoted")
        definition = await self._session.scalar(
            select(StrategyDefinition)
            .where(StrategyDefinition.id == version.definition_id)
            .with_for_update()
        )
        if definition is None:
            raise LookupError("strategy definition not found")
        if definition.research_only or version.research_only:
            raise ValueError("research-only strategy cannot be live approved")
        hard_rule_count = await self._session.scalar(
            select(func.count(StrategyRule.id)).where(
                StrategyRule.strategy_version_id == strategy_version_id,
                StrategyRule.hard_rule.is_(True),
            )
        )
        verified_rule_count = await self._session.scalar(
            select(func.count(func.distinct(StrategyRule.id)))
            .join(StrategyRule, StrategyRule.id == StrategyRuleSource.rule_id)
            .join(
                StrategySourceReference,
                StrategySourceReference.id == StrategyRuleSource.source_reference_id,
            )
            .where(
                StrategyRule.strategy_version_id == strategy_version_id,
                StrategyRule.hard_rule.is_(True),
                StrategySourceReference.verified.is_(True),
            )
        )
        if (hard_rule_count or 0) != (verified_rule_count or 0):
            raise ValueError("every enabled hard rule requires a verified source")
        version.status = StrategyStatus.LIVE_APPROVED
        await self._session.flush()
