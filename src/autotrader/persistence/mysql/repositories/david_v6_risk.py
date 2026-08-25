from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.risk import (
    DavidV6SessionRiskAnchorRow,
    RiskPolicy,
    RiskPolicyVersion,
    RiskSnapshot,
)
from autotrader.risk.models import V6RiskPolicySnapshot, V6SessionRiskAnchor
from autotrader.strategies.david_v6.models import V6Market

V6_RISK_POLICY_VERSION = "v6-op-20260824.1"


@dataclass(frozen=True, slots=True)
class V6RiskPolicyDefinition:
    code: str
    version: str
    market: V6Market
    currency: str | None
    settlement_asset: str | None
    normal_risk_fraction: Decimal
    a_candidate_risk_fraction: Decimal | None
    a_risk_fraction: Decimal
    absolute_trade_risk_fraction: Decimal
    daily_loss_fraction: Decimal
    weekly_loss_fraction: Decimal
    max_consecutive_losses: int
    max_open_structural_risk_fraction: Decimal
    account_age_seconds: int
    risk_age_seconds: int
    quote_age_seconds: int
    provider_age_seconds: int
    stream_gap_age_seconds: int | None
    completed_intraday_bar_arrival_seconds: int
    daily_requires_authoritative_close: bool

    def snapshot(self, policy_version_id: UUID) -> V6RiskPolicySnapshot:
        return V6RiskPolicySnapshot(
            policy_version_id=policy_version_id,
            market=self.market,
            normal_risk_fraction=self.normal_risk_fraction,
            a_candidate_risk_fraction=self.a_candidate_risk_fraction,
            a_risk_fraction=self.a_risk_fraction,
            absolute_trade_risk_fraction=self.absolute_trade_risk_fraction,
            daily_loss_fraction=self.daily_loss_fraction,
            weekly_loss_fraction=self.weekly_loss_fraction,
            max_consecutive_losses=self.max_consecutive_losses,
            max_open_structural_risk_fraction=(self.max_open_structural_risk_fraction),
            account_age=timedelta(seconds=self.account_age_seconds),
            risk_age=timedelta(seconds=self.risk_age_seconds),
            quote_age=timedelta(seconds=self.quote_age_seconds),
            provider_age=timedelta(seconds=self.provider_age_seconds),
            stream_gap_age=(
                None
                if self.stream_gap_age_seconds is None
                else timedelta(seconds=self.stream_gap_age_seconds)
            ),
            completed_intraday_bar_arrival_age=timedelta(
                seconds=self.completed_intraday_bar_arrival_seconds
            ),
            daily_requires_authoritative_close=(
                self.daily_requires_authoritative_close
            ),
        )


APPROVED_V6_RISK_POLICIES = (
    V6RiskPolicyDefinition(
        code="DAVID_V6_CASH_KRW",
        version=V6_RISK_POLICY_VERSION,
        market=V6Market.KRX_CASH,
        currency="KRW",
        settlement_asset=None,
        normal_risk_fraction=Decimal("0.0015"),
        a_candidate_risk_fraction=None,
        a_risk_fraction=Decimal("0.0025"),
        absolute_trade_risk_fraction=Decimal("0.0025"),
        daily_loss_fraction=Decimal("0.0075"),
        weekly_loss_fraction=Decimal("0.0200"),
        max_consecutive_losses=2,
        max_open_structural_risk_fraction=Decimal("0.0075"),
        account_age_seconds=30,
        risk_age_seconds=5,
        quote_age_seconds=3,
        provider_age_seconds=30,
        stream_gap_age_seconds=None,
        completed_intraday_bar_arrival_seconds=90,
        daily_requires_authoritative_close=True,
    ),
    V6RiskPolicyDefinition(
        code="DAVID_V6_CASH_USD",
        version=V6_RISK_POLICY_VERSION,
        market=V6Market.US_CASH,
        currency="USD",
        settlement_asset=None,
        normal_risk_fraction=Decimal("0.0015"),
        a_candidate_risk_fraction=None,
        a_risk_fraction=Decimal("0.0025"),
        absolute_trade_risk_fraction=Decimal("0.0025"),
        daily_loss_fraction=Decimal("0.0075"),
        weekly_loss_fraction=Decimal("0.0200"),
        max_consecutive_losses=2,
        max_open_structural_risk_fraction=Decimal("0.0075"),
        account_age_seconds=30,
        risk_age_seconds=5,
        quote_age_seconds=3,
        provider_age_seconds=30,
        stream_gap_age_seconds=None,
        completed_intraday_bar_arrival_seconds=90,
        daily_requires_authoritative_close=True,
    ),
    V6RiskPolicyDefinition(
        code="DAVID_V6_BINANCE_USDM_USDT",
        version=V6_RISK_POLICY_VERSION,
        market=V6Market.BINANCE_USDM,
        currency=None,
        settlement_asset="USDT",
        normal_risk_fraction=Decimal("0.0025"),
        a_candidate_risk_fraction=Decimal("0.0025"),
        a_risk_fraction=Decimal("0.0050"),
        absolute_trade_risk_fraction=Decimal("0.0075"),
        daily_loss_fraction=Decimal("0.0075"),
        weekly_loss_fraction=Decimal("0.0200"),
        max_consecutive_losses=2,
        max_open_structural_risk_fraction=Decimal("0.0075"),
        account_age_seconds=5,
        risk_age_seconds=2,
        quote_age_seconds=1,
        provider_age_seconds=5,
        stream_gap_age_seconds=2,
        completed_intraday_bar_arrival_seconds=2,
        daily_requires_authoritative_close=False,
    ),
)

_DEFINITIONS_BY_CODE = {item.code: item for item in APPROVED_V6_RISK_POLICIES}


class DavidV6RiskRepository:
    """Loads exact v6 percentage policy and immutable session risk anchors."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_active_policy(
        self, *, code: str, market: V6Market
    ) -> V6RiskPolicySnapshot | None:
        definition = _exact_definition(code=code, market=market)
        row = await self._session.scalar(
            select(RiskPolicyVersion)
            .join(RiskPolicy, RiskPolicy.id == RiskPolicyVersion.policy_id)
            .where(
                RiskPolicy.code == definition.code,
                RiskPolicy.active.is_(True),
                RiskPolicyVersion.version == definition.version,
                RiskPolicyVersion.active.is_(True),
            )
            .with_for_update()
        )
        if row is None:
            return None
        _require_exact_policy_row(row, definition)
        return definition.snapshot(row.id)

    async def persist_session_anchor(
        self, anchor: V6SessionRiskAnchor
    ) -> V6SessionRiskAnchor:
        if type(anchor) is not V6SessionRiskAnchor:
            raise ValueError("exact V6SessionRiskAnchor is required")
        anchor.__post_init__()
        policy_row = await self._session.scalar(
            select(RiskPolicyVersion)
            .where(RiskPolicyVersion.id == anchor.policy_version_id)
            .with_for_update()
        )
        policy = None
        if policy_row is not None:
            policy = await self._session.scalar(
                select(RiskPolicy)
                .where(RiskPolicy.id == policy_row.policy_id)
                .with_for_update()
            )
        snapshot = await self._session.scalar(
            select(RiskSnapshot)
            .where(RiskSnapshot.id == anchor.risk_snapshot_id)
            .with_for_update()
        )
        if (
            policy_row is None
            or policy is None
            or not policy_row.active
            or not policy.active
        ):
            raise ValueError("session anchor requires an active policy version")
        definition = _exact_definition(code=policy.code, market=anchor.market)
        _require_exact_policy_row(policy_row, definition)
        if (
            snapshot is None
            or snapshot.account_id != anchor.account_id
            or snapshot.account_snapshot_id != anchor.account_snapshot_id
            or snapshot.equity != anchor.starting_equity
            or snapshot.currency != anchor.currency
            or snapshot.settlement_asset != anchor.settlement_asset
            or snapshot.as_of > anchor.captured_at
        ):
            raise ValueError("session anchor requires its exact risk snapshot")
        existing = await self._session.scalar(
            select(DavidV6SessionRiskAnchorRow)
            .where(
                DavidV6SessionRiskAnchorRow.account_id == anchor.account_id,
                DavidV6SessionRiskAnchorRow.market == anchor.market.value,
                DavidV6SessionRiskAnchorRow.session_key == anchor.session_key,
            )
            .with_for_update()
        )
        if existing is not None:
            _require_matching_anchor(existing, anchor)
            return anchor
        self._session.add(
            DavidV6SessionRiskAnchorRow(
                id=anchor.id,
                account_id=anchor.account_id,
                policy_version_id=anchor.policy_version_id,
                account_snapshot_id=anchor.account_snapshot_id,
                risk_snapshot_id=anchor.risk_snapshot_id,
                market=anchor.market.value,
                session_key=anchor.session_key,
                session_started_at=anchor.session_started_at,
                captured_at=anchor.captured_at,
                starting_equity=anchor.starting_equity,
                currency=anchor.currency,
                settlement_asset=anchor.settlement_asset,
                evidence_hash=anchor.evidence_hash,
            )
        )
        await self._session.flush()
        return anchor


def _exact_definition(*, code: str, market: V6Market) -> V6RiskPolicyDefinition:
    if type(market) is not V6Market:
        raise TypeError("market must be an exact V6Market")
    definition = _DEFINITIONS_BY_CODE.get(code)
    if definition is None or definition.market is not market:
        raise ValueError("unapproved v6 risk policy scope")
    return definition


def _require_exact_policy_row(
    row: RiskPolicyVersion, definition: V6RiskPolicyDefinition
) -> None:
    expected = {
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
    }
    if any(getattr(row, name) != value for name, value in expected.items()):
        raise ValueError("persisted v6 risk policy differs from approved authority")
    if any(
        getattr(row, name) is not None
        for name in (
            "max_total_risk",
            "max_position_value",
            "max_daily_loss",
            "max_drawdown",
            "max_account_snapshot_age_seconds",
            "max_risk_snapshot_age_seconds",
            "max_market_data_age_seconds",
            "max_provider_fact_age_seconds",
        )
    ):
        raise ValueError("v6 percentage policy cannot contain an absolute cap")


def _require_matching_anchor(
    row: DavidV6SessionRiskAnchorRow, anchor: V6SessionRiskAnchor
) -> None:
    expected = {
        "id": anchor.id,
        "policy_version_id": anchor.policy_version_id,
        "account_snapshot_id": anchor.account_snapshot_id,
        "risk_snapshot_id": anchor.risk_snapshot_id,
        "session_started_at": anchor.session_started_at,
        "captured_at": anchor.captured_at,
        "starting_equity": anchor.starting_equity,
        "currency": anchor.currency,
        "settlement_asset": anchor.settlement_asset,
        "evidence_hash": anchor.evidence_hash,
    }
    if any(getattr(row, name) != value for name, value in expected.items()):
        raise ValueError("session risk anchor identity payload collision")
