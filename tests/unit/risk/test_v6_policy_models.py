from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from autotrader.persistence.mysql.repositories.david_v6_risk import (
    APPROVED_V6_RISK_POLICIES,
)
from autotrader.risk.models import (
    RiskSnapshotView,
    V6RiskPolicySnapshot,
    V6SessionRiskAnchor,
)
from autotrader.strategies.david_v6.models import SetupGrade, V6Market

POLICY_VERSION_ID = UUID("019d0000-0000-7000-8000-000000000101")
ANCHOR_ID = UUID("019d0000-0000-7000-8000-000000000102")
ACCOUNT_ID = UUID("019d0000-0000-7000-8000-000000000103")
ACCOUNT_SNAPSHOT_ID = UUID("019d0000-0000-7000-8000-000000000104")
RISK_SNAPSHOT_ID = UUID("019d0000-0000-7000-8000-000000000106")
SESSION_STARTED_AT = datetime(2026, 8, 24, tzinfo=UTC)


def _binance_policy() -> V6RiskPolicySnapshot:
    definition = next(
        item
        for item in APPROVED_V6_RISK_POLICIES
        if item.code == "DAVID_V6_BINANCE_USDM_USDT"
    )
    return definition.snapshot(POLICY_VERSION_ID)


def _futures_risk_snapshot() -> RiskSnapshotView:
    return RiskSnapshotView(
        id=UUID("019d0000-0000-7000-8000-000000000105"),
        account_id=ACCOUNT_ID,
        as_of=SESSION_STARTED_AT,
        currency=None,
        equity=Decimal("2000"),
        cash=Decimal("2000"),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        open_risk=Decimal("0"),
        daily_realized_pnl=Decimal("0"),
        daily_unrealized_pnl=Decimal("0"),
        drawdown=Decimal("0"),
        position_hash=b"p" * 32,
        open_order_hash=b"o" * 32,
        settlement_asset="USDT",
    )


def test_binance_freshness_accepts_exact_equality() -> None:
    policy = _binance_policy()

    assert policy.is_account_fresh(age=timedelta(seconds=5))
    assert not policy.is_account_fresh(age=timedelta(seconds=5, microseconds=1))
    assert policy.is_risk_fresh(age=timedelta(seconds=2))
    assert not policy.is_risk_fresh(age=timedelta(seconds=2, microseconds=1))
    assert policy.is_quote_fresh(age=timedelta(seconds=1))
    assert not policy.is_quote_fresh(age=timedelta(seconds=1, microseconds=1))
    assert policy.is_provider_fresh(age=timedelta(seconds=5))
    assert not policy.is_provider_fresh(age=timedelta(seconds=5, microseconds=1))
    assert policy.is_stream_gap_fresh(age=timedelta(seconds=2))
    assert not policy.is_stream_gap_fresh(age=timedelta(seconds=2, microseconds=1))
    assert policy.is_completed_intraday_bar_fresh(age=timedelta(seconds=2))
    assert not policy.is_completed_intraday_bar_fresh(
        age=timedelta(seconds=2, microseconds=1)
    )


def test_exact_approved_v6_risk_policies_have_no_absolute_cap() -> None:
    definitions = {item.code: item for item in APPROVED_V6_RISK_POLICIES}

    assert set(definitions) == {
        "DAVID_V6_CASH_KRW",
        "DAVID_V6_CASH_USD",
        "DAVID_V6_BINANCE_USDM_USDT",
    }
    for code in ("DAVID_V6_CASH_KRW", "DAVID_V6_CASH_USD"):
        policy = definitions[code].snapshot(POLICY_VERSION_ID)
        assert policy.normal_risk_fraction == Decimal("0.0015")
        assert policy.a_candidate_risk_fraction is None
        assert policy.a_risk_fraction == Decimal("0.0025")
        assert policy.absolute_trade_risk_fraction == Decimal("0.0025")
        assert policy.account_age == timedelta(seconds=30)
        assert policy.risk_age == timedelta(seconds=5)
        assert policy.quote_age == timedelta(seconds=3)
        assert policy.provider_age == timedelta(seconds=30)
        assert policy.stream_gap_age is None
        assert policy.completed_intraday_bar_arrival_age == timedelta(seconds=90)
        assert policy.daily_requires_authoritative_close is True

    binance = definitions["DAVID_V6_BINANCE_USDM_USDT"].snapshot(POLICY_VERSION_ID)
    # Section 21's stated figures, which this market had been running above.
    assert binance.normal_risk_fraction == Decimal("0.0015")
    assert binance.a_candidate_risk_fraction == Decimal("0.0025")
    assert binance.a_risk_fraction == Decimal("0.0025")
    # Untouched: the document names no absolute per-trade ceiling. It now
    # sits five times the normal fraction, so it binds on nothing - it is a
    # validation bound on the grades, not an input to sizing.
    assert binance.absolute_trade_risk_fraction == Decimal("0.0075")
    assert binance.risk_fraction_for(SetupGrade.A) == Decimal("0.0025")
    assert binance.daily_requires_authoritative_close is False

    for definition in definitions.values():
        policy = definition.snapshot(POLICY_VERSION_ID)
        assert policy.daily_loss_fraction == Decimal("0.0075")
        assert policy.weekly_loss_fraction == Decimal("0.0200")
        assert policy.max_consecutive_losses == 2
        assert policy.max_open_structural_risk_fraction == Decimal("0.0075")
        assert not hasattr(policy, "absolute_cap")


def test_futures_uses_settlement_asset_instead_of_cash_currency() -> None:
    snapshot = _futures_risk_snapshot()

    assert snapshot.settlement_asset == "USDT"
    assert snapshot.currency is None


@pytest.mark.parametrize(
    ("currency", "settlement_asset"),
    ((None, None), ("USD", "USDT")),
)
def test_asset_denomination_requires_exactly_one_authority(
    currency: str | None, settlement_asset: str | None
) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        replace(
            _futures_risk_snapshot(),
            currency=currency,
            settlement_asset=settlement_asset,
        )


def test_session_risk_anchor_uses_lower_of_start_and_current_equity() -> None:
    anchor = V6SessionRiskAnchor(
        id=ANCHOR_ID,
        account_id=ACCOUNT_ID,
        policy_version_id=POLICY_VERSION_ID,
        account_snapshot_id=ACCOUNT_SNAPSHOT_ID,
        risk_snapshot_id=RISK_SNAPSHOT_ID,
        market=V6Market.BINANCE_USDM,
        session_key="2026-08-24",
        session_started_at=SESSION_STARTED_AT,
        captured_at=SESSION_STARTED_AT + timedelta(seconds=1),
        starting_equity=Decimal("2000"),
        currency=None,
        settlement_asset="USDT",
        evidence_hash=b"e" * 32,
    )

    assert anchor.risk_base(current_equity=Decimal("2100")) == Decimal("2000")
    assert anchor.risk_base(current_equity=Decimal("1800")) == Decimal("1800")
