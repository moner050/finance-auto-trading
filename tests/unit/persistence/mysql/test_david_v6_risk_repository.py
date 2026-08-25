from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from autotrader.persistence.mysql.models.risk import (
    DavidV6SessionRiskAnchorRow,
    RiskPolicy,
    RiskPolicyVersion,
    RiskSnapshot,
)
from autotrader.persistence.mysql.repositories.david_v6_risk import (
    APPROVED_V6_RISK_POLICIES,
    DavidV6RiskRepository,
)
from autotrader.risk.models import V6SessionRiskAnchor
from autotrader.strategies.david_v6.models import V6Market

POLICY_ID = UUID("019d0000-0000-7000-8000-000000000201")
POLICY_VERSION_ID = UUID("019d0000-0000-7000-8000-000000000202")
ACCOUNT_ID = UUID("019d0000-0000-7000-8000-000000000203")
ACCOUNT_SNAPSHOT_ID = UUID("019d0000-0000-7000-8000-000000000204")
RISK_SNAPSHOT_ID = UUID("019d0000-0000-7000-8000-000000000205")
ANCHOR_ID = UUID("019d0000-0000-7000-8000-000000000206")
NOW = datetime(2026, 8, 24, tzinfo=UTC)


class FakeSession:
    def __init__(self, values: list[object | None]) -> None:
        self._values = values
        self.added: list[object] = []
        self.flushed = False

    async def scalar(self, statement: object) -> object | None:
        del statement
        return self._values.pop(0)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushed = True


def _definition() -> Any:
    return next(
        item
        for item in APPROVED_V6_RISK_POLICIES
        if item.code == "DAVID_V6_BINANCE_USDM_USDT"
    )


def _policy_version(**changes: object) -> RiskPolicyVersion:
    definition = _definition()
    values: dict[str, object] = {
        "id": POLICY_VERSION_ID,
        "policy_id": POLICY_ID,
        "version": definition.version,
        "active": True,
        "max_total_risk": None,
        "max_position_value": None,
        "max_daily_loss": None,
        "max_drawdown": None,
        "normal_risk_fraction": definition.normal_risk_fraction,
        "a_candidate_risk_fraction": definition.a_candidate_risk_fraction,
        "a_risk_fraction": definition.a_risk_fraction,
        "absolute_trade_risk_fraction": definition.absolute_trade_risk_fraction,
        "daily_loss_fraction": definition.daily_loss_fraction,
        "weekly_loss_fraction": definition.weekly_loss_fraction,
        "max_consecutive_losses": definition.max_consecutive_losses,
        "max_open_structural_risk_fraction": (
            definition.max_open_structural_risk_fraction
        ),
        "account_age_seconds": definition.account_age_seconds,
        "risk_age_seconds": definition.risk_age_seconds,
        "quote_age_seconds": definition.quote_age_seconds,
        "provider_age_seconds": definition.provider_age_seconds,
        "stream_gap_age_seconds": definition.stream_gap_age_seconds,
        "completed_intraday_bar_arrival_seconds": (
            definition.completed_intraday_bar_arrival_seconds
        ),
        "daily_requires_authoritative_close": (
            definition.daily_requires_authoritative_close
        ),
        "max_account_snapshot_age_seconds": None,
        "max_risk_snapshot_age_seconds": None,
        "max_market_data_age_seconds": None,
        "max_provider_fact_age_seconds": None,
    }
    values.update(changes)
    return RiskPolicyVersion(**values)


def _policy() -> RiskPolicy:
    return RiskPolicy(
        id=POLICY_ID,
        code="DAVID_V6_BINANCE_USDM_USDT",
        active=True,
    )


def _risk_snapshot() -> RiskSnapshot:
    return RiskSnapshot(
        id=RISK_SNAPSHOT_ID,
        account_snapshot_id=ACCOUNT_SNAPSHOT_ID,
        account_id=ACCOUNT_ID,
        as_of=NOW,
        currency=None,
        settlement_asset="USDT",
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
    )


def _anchor(**changes: object) -> V6SessionRiskAnchor:
    values: dict[str, object] = {
        "id": ANCHOR_ID,
        "account_id": ACCOUNT_ID,
        "policy_version_id": POLICY_VERSION_ID,
        "account_snapshot_id": ACCOUNT_SNAPSHOT_ID,
        "risk_snapshot_id": RISK_SNAPSHOT_ID,
        "market": V6Market.BINANCE_USDM,
        "session_key": "2026-08-24",
        "session_started_at": NOW,
        "captured_at": NOW + timedelta(seconds=1),
        "starting_equity": Decimal("2000"),
        "currency": None,
        "settlement_asset": "USDT",
        "evidence_hash": b"e" * 32,
    }
    values.update(changes)
    return V6SessionRiskAnchor(**values)


@pytest.mark.asyncio
async def test_load_active_policy_accepts_only_exact_persisted_values() -> None:
    exact_session = FakeSession([_policy_version()])
    exact = await DavidV6RiskRepository(exact_session).load_active_policy(
        code="DAVID_V6_BINANCE_USDM_USDT",
        market=V6Market.BINANCE_USDM,
    )

    assert exact is not None
    assert exact.policy_version_id == POLICY_VERSION_ID

    drifted_session = FakeSession(
        [_policy_version(daily_loss_fraction=Decimal("0.0080"))]
    )
    with pytest.raises(ValueError, match="differs"):
        await DavidV6RiskRepository(drifted_session).load_active_policy(
            code="DAVID_V6_BINANCE_USDM_USDT",
            market=V6Market.BINANCE_USDM,
        )


@pytest.mark.asyncio
async def test_persist_session_anchor_requires_exact_policy_and_risk_snapshot() -> None:
    session = FakeSession([_policy_version(), _policy(), _risk_snapshot(), None])

    persisted = await DavidV6RiskRepository(session).persist_session_anchor(_anchor())

    assert persisted == _anchor()
    assert session.flushed is True
    assert len(session.added) == 1
    row = session.added[0]
    assert isinstance(row, DavidV6SessionRiskAnchorRow)
    assert row.risk_snapshot_id == RISK_SNAPSHOT_ID
    assert row.currency is None
    assert row.settlement_asset == "USDT"


@pytest.mark.asyncio
async def test_persist_session_anchor_rejects_unproven_starting_equity() -> None:
    session = FakeSession([_policy_version(), _policy(), _risk_snapshot()])

    with pytest.raises(ValueError, match="exact risk snapshot"):
        await DavidV6RiskRepository(session).persist_session_anchor(
            _anchor(starting_equity=Decimal("1999"))
        )

    assert session.added == []
