from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid7

import pytest

from autotrader.domain.enums import IntentType, OrderStyle, Side
from autotrader.execution.intents.models import (
    AccountCandidate,
    IntentOrigin,
    MarketQuote,
    OperatorRequest,
    OrderTerms,
    ProtectionRequest,
    ReconciliationRequest,
    SizingApproved,
)
from autotrader.execution.intents.service import AccountRouter, OrderIntentFactory
from autotrader.strategies.common.decisions import StrategyDecision

NOW = datetime(2026, 8, 9, tzinfo=UTC)


def strategy_decision(
    *, order_style: OrderStyle = OrderStyle.LIMIT
) -> StrategyDecision:
    return StrategyDecision(
        id=uuid7(),
        strategy_version_id=uuid7(),
        setup_id=uuid7(),
        feature_snapshot_id=uuid7(),
        instrument_id=uuid7(),
        intent_type=IntentType.ENTRY,
        side=Side.BUY,
        order_style=order_style,
        planned_entry=Decimal("10"),
        trigger_price=Decimal("10"),
        invalidation_price=Decimal("8"),
        generated_at=NOW,
        valid_until=NOW + timedelta(minutes=1),
        session_type="REGULAR",
    )


def test_strategy_intent_uses_only_approved_sizing_and_stable_identity() -> None:
    account = AccountCandidate(
        id=uuid7(),
        broker_code="TEST",
        market_code="KR",
        environment="PAPER",
        enabled=True,
        policy_key="default",
        policy_active=True,
    )
    decision = strategy_decision()
    intent = OrderIntentFactory().from_strategy_decision(
        decision=decision,
        account=account,
        sizing=SizingApproved(quantity=Decimal("2")),
    )

    assert intent.quantity == Decimal("2")
    assert intent.idempotency_key == f"strategy:{decision.id.hex}:{account.id.hex}"
    assert intent.instrument_id == decision.instrument_id
    assert intent.intent_type is decision.intent_type
    assert decision.planned_entry == Decimal("10")


def test_market_intent_requires_fresh_side_specific_quote() -> None:
    account = AccountCandidate(
        id=uuid7(),
        broker_code="TEST",
        market_code="KR",
        environment="PAPER",
        enabled=True,
        policy_key="default",
        policy_active=True,
    )
    with pytest.raises(ValueError, match="fresh"):
        OrderIntentFactory().from_strategy_decision(
            decision=strategy_decision(order_style=OrderStyle.MARKET),
            account=account,
            sizing=SizingApproved(quantity=Decimal("1")),
            quote=MarketQuote(bid=Decimal("9"), ask=Decimal("10"), fresh=False),
        )


def test_router_requires_exact_enabled_market_environment() -> None:
    requested = AccountCandidate(
        id=uuid7(),
        broker_code="TEST",
        market_code="KR",
        environment="PAPER",
        enabled=True,
        policy_key="default",
        policy_active=True,
    )
    mismatch = AccountCandidate(
        id=uuid7(),
        broker_code="TEST",
        market_code="US",
        environment="PAPER",
        enabled=True,
        policy_key="default",
        policy_active=True,
    )

    assert (
        AccountRouter().route(
            (mismatch, requested),
            broker_code="TEST",
            market_code="KR",
            environment="PAPER",
            policy_key="default",
        )
        == requested
    )

    inactive = AccountCandidate(
        id=uuid7(),
        broker_code="TEST",
        market_code="KR",
        environment="PAPER",
        enabled=True,
        policy_key="default",
        policy_active=False,
    )
    with pytest.raises(ValueError, match="exactly one"):
        AccountRouter().route(
            (inactive,),
            broker_code="TEST",
            market_code="KR",
            environment="PAPER",
            policy_key="default",
        )


def test_operator_intent_requires_audit_evidence_and_canonical_identity() -> None:
    account = AccountCandidate(
        id=uuid7(),
        broker_code="TEST",
        market_code="KR",
        environment="PAPER",
        enabled=True,
        policy_key="default",
        policy_active=True,
    )
    audit_id = uuid7()
    intent = OrderIntentFactory().from_operator(
        request=OperatorRequest(
            audit_id=audit_id,
            instrument_id=uuid7(),
            intent_type=IntentType.EXIT,
            side=Side.SELL,
            order_style=OrderStyle.LIMIT,
            terms=OrderTerms(
                requested_quantity=Decimal("1"), limit_price=Decimal("10")
            ),
        ),
        account=account,
    )

    assert intent.origin is IntentOrigin.OPERATOR
    assert intent.idempotency_key == f"operator:{audit_id.hex}:{account.id.hex}"


def test_protection_and_reconciliation_require_typed_evidence() -> None:
    account = AccountCandidate(
        id=uuid7(),
        broker_code="TEST",
        market_code="KR",
        environment="PAPER",
        enabled=True,
        policy_key="default",
        policy_active=True,
    )
    factory = OrderIntentFactory()
    protection_id = uuid7()
    protection = factory.from_protection(
        account=account,
        request=ProtectionRequest(
            locked_position_id=protection_id,
            reason_code="STOP_LOSS",
            instrument_id=uuid7(),
            intent_type=IntentType.PROTECTIVE,
            side=Side.SELL,
            order_style=OrderStyle.MARKET,
            terms=OrderTerms(requested_quantity=Decimal("1"), limit_price=None),
            quote=MarketQuote(bid=Decimal("9"), ask=Decimal("10"), fresh=True),
        ),
    )
    diff_id = uuid7()
    reconciliation = factory.from_reconciliation(
        account=account,
        request=ReconciliationRequest(
            blocking_diff_id=diff_id,
            instrument_id=uuid7(),
            intent_type=IntentType.EXIT,
            side=Side.BUY,
            order_style=OrderStyle.LIMIT,
            terms=OrderTerms(
                requested_quantity=Decimal("1"), limit_price=Decimal("10")
            ),
        ),
    )

    # The reason is part of it: one position carries a structural stop, its
    # exits and an emergency close, and keyed on the position alone they
    # collide - `create_or_get` would hand the second one the first's intent.
    assert protection.idempotency_key == (
        f"protection:{protection_id.hex}:{account.id.hex}:STOP_LOSS"
    )
    assert reconciliation.idempotency_key == (
        f"reconciliation:{diff_id.hex}:{account.id.hex}"
    )


def test_protection_rejects_blank_reason_code() -> None:
    with pytest.raises(ValueError, match="reason_code"):
        ProtectionRequest(
            locked_position_id=uuid7(),
            reason_code=" ",
            instrument_id=uuid7(),
            intent_type=IntentType.PROTECTIVE,
            side=Side.SELL,
            order_style=OrderStyle.MARKET,
            terms=OrderTerms(requested_quantity=Decimal("1"), limit_price=None),
        )
