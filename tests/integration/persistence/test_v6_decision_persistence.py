from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from autotrader.domain.enums import IntentType, OrderStyle, Side
from autotrader.shared.ids import new_uuid7
from autotrader.strategies.common.decisions import StrategyDecision


def test_generic_decision_retains_the_originating_v6_decision_id() -> None:
    decision_id = new_uuid7()
    now = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)

    decision = StrategyDecision(
        id=decision_id,
        strategy_version_id=new_uuid7(),
        setup_id=new_uuid7(),
        feature_snapshot_id=new_uuid7(),
        instrument_id=new_uuid7(),
        intent_type=IntentType.ENTRY,
        side=Side.BUY,
        order_style=OrderStyle.LIMIT,
        planned_entry=Decimal("100"),
        trigger_price=Decimal("100"),
        invalidation_price=Decimal("99"),
        generated_at=now,
        valid_until=now + timedelta(minutes=5),
        session_type="BINANCE_USDM",
        source_v6_decision_id=decision_id,
    )

    assert decision.source_v6_decision_id == decision.id
    assert decision.decision_hash() == decision.decision_hash()
