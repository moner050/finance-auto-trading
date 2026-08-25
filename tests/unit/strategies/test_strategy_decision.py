from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid7

import pytest

from autotrader.domain.enums import IntentType, OrderStyle, Side
from autotrader.strategies.common.decisions import (
    StrategyDecision,
    StrategyStatus,
    validate_strategy_promotion,
)

NOW = datetime(2026, 8, 9, tzinfo=UTC)


def decision() -> StrategyDecision:
    return StrategyDecision(
        id=uuid7(),
        strategy_version_id=uuid7(),
        setup_id=uuid7(),
        feature_snapshot_id=uuid7(),
        instrument_id=uuid7(),
        intent_type=IntentType.ENTRY,
        side=Side.BUY,
        order_style=OrderStyle.LIMIT,
        planned_entry=Decimal("100"),
        trigger_price=Decimal("101"),
        invalidation_price=Decimal("99"),
        generated_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
        session_type="REGULAR",
    )


def test_strategy_decision_is_account_and_quantity_free_with_stable_hash() -> None:
    first = decision()

    same = StrategyDecision(
        **{field.name: getattr(first, field.name) for field in fields(first)}
    )

    field_names = {field.name for field in fields(first)}
    assert "account_id" not in field_names
    assert "quantity" not in field_names
    assert first.decision_hash() == same.decision_hash()


def test_legacy_strategy_decision_retains_pre_v6_authority_hash() -> None:
    legacy = StrategyDecision(
        id=UUID("0198f000-0000-7000-8000-000000000001"),
        strategy_version_id=UUID("0198f000-0000-7000-8000-000000000002"),
        setup_id=UUID("0198f000-0000-7000-8000-000000000003"),
        feature_snapshot_id=UUID("0198f000-0000-7000-8000-000000000004"),
        instrument_id=UUID("0198f000-0000-7000-8000-000000000005"),
        intent_type=IntentType.ENTRY,
        side=Side.BUY,
        order_style=OrderStyle.LIMIT,
        planned_entry=Decimal("100"),
        trigger_price=Decimal("101"),
        invalidation_price=Decimal("99"),
        generated_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
        session_type="REGULAR",
    )

    assert legacy.decision_hash().hex() == (
        "ce1332cfad49bac2de0ea764c3734bf2472c80c1523de4172a831195309b4131"
    )


def test_research_only_strategy_can_never_be_live_approved() -> None:
    with pytest.raises(ValueError, match="research-only"):
        validate_strategy_promotion(
            status=StrategyStatus.LIVE_APPROVED,
            research_only=True,
            enabled_hard_rule_count=0,
            verified_source_link_count=0,
        )


def test_live_approval_requires_a_verified_source_for_every_hard_rule() -> None:
    with pytest.raises(ValueError, match="verified source"):
        validate_strategy_promotion(
            status=StrategyStatus.LIVE_APPROVED,
            research_only=False,
            enabled_hard_rule_count=2,
            verified_source_link_count=1,
        )


@pytest.mark.parametrize(
    "field_name", ["planned_entry", "trigger_price", "invalidation_price"]
)
def test_strategy_decision_rejects_nonpositive_price(field_name: str) -> None:
    values = {
        "planned_entry": Decimal("100"),
        "trigger_price": Decimal("101"),
        "invalidation_price": Decimal("99"),
    }
    values[field_name] = Decimal("0")

    with pytest.raises(ValueError, match="positive"):
        StrategyDecision(
            id=uuid7(),
            strategy_version_id=uuid7(),
            setup_id=uuid7(),
            feature_snapshot_id=uuid7(),
            instrument_id=uuid7(),
            intent_type=IntentType.ENTRY,
            side=Side.BUY,
            order_style=OrderStyle.LIMIT,
            planned_entry=values["planned_entry"],
            trigger_price=values["trigger_price"],
            invalidation_price=values["invalidation_price"],
            generated_at=NOW,
            valid_until=NOW + timedelta(minutes=5),
            session_type="REGULAR",
        )
