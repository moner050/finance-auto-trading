from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.risk.v6 import V6RiskContext, V6RiskRequest
from autotrader.shared.ids import new_uuid7
from autotrader.strategies.david_v6.calendar import CalendarFacts
from autotrader.strategies.david_v6.costs import CostFacts
from autotrader.strategies.david_v6.engine import evaluate_v6
from autotrader.strategies.david_v6.evidence import (
    EvidenceItem,
    EvidenceProvenance,
    V6EvidenceBundle,
)
from autotrader.strategies.david_v6.exhaustion import ExhaustionFacts
from autotrader.strategies.david_v6.grading import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
)
from autotrader.strategies.david_v6.manifest import (
    V6_DESIGN_SHA256,
    V6_SOURCE_SHA256,
    V6Manifest,
    v6_configuration_hash,
)
from autotrader.strategies.david_v6.metodo import MetodoFacts
from autotrader.strategies.david_v6.models import (
    EvidenceState,
    MatchedIndicator,
    SetupGrade,
    StrategyFamily,
    V6Market,
)
from autotrader.strategies.david_v6.order_flow import OrderFlowFacts
from autotrader.strategies.david_v6.pivots import DivergenceFacts
from autotrader.strategies.david_v6.profile import ProfileFacts
from autotrader.strategies.david_v6.regime import RegimeFacts, RegimeLabel
from autotrader.strategies.david_v6.sessions import SessionFacts
from autotrader.strategies.david_v6.universe import UniverseFacts
from autotrader.strategies.david_v6.zones import ZoneFacts

NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)
FACT_KEYS = (
    "universe",
    "regime",
    "metodo",
    "zones",
    "divergence",
    "exhaustion",
    "order_flow",
    "profile",
    "calendar",
    "session",
    "costs",
)


def _provenance(key: str) -> EvidenceProvenance:
    return EvidenceProvenance(
        source="TEST",
        source_key=key,
        source_timezone="UTC",
        observed_at=NOW - timedelta(seconds=1),
        captured_at=NOW,
        digest_sha256=bytes(key, "utf-8").hex().ljust(64, "0")[:64],
    )


def _item(key: str) -> EvidenceItem[object]:
    return EvidenceItem(
        state=EvidenceState.AVAILABLE,
        value=_fact_value(key),
        provenance=_provenance(key),
        blocker_code=None,
    )


def _fact_value(key: str) -> object:
    if key == "universe":
        return UniverseFacts(
            member_as_of=True,
            common_stock_as_of=True,
            median_value_20d=Decimal("100"),
            cross_section_median_value_20d=Decimal("50"),
            sector_return_70d_rank=1,
            sector_classification="technology",
            eligible=True,
            blockers=(),
        )
    if key == "regime":
        return RegimeFacts(
            state=EvidenceState.AVAILABLE,
            trend=RegimeLabel.TREND_UP,
            sideways=False,
            low_volatility=False,
            pessimism_extreme=False,
            excluded=False,
        )
    if key == "metodo":
        return MetodoFacts(
            observed_at=NOW,
            sma_6=Decimal("103"),
            sma_70=Decimal("102"),
            sma_200=Decimal("100"),
            sma_70_slope=Decimal("1"),
            sma_200_slope=Decimal("1"),
            trend_up=True,
            trend_down=False,
            sma_6_70_cross_up=True,
            sma_6_70_cross_down=False,
            macd=Decimal("1"),
            macd_signal=Decimal("0.5"),
            macd_cross_up_above_zero=True,
            latest_volume=Decimal("100"),
            mean_volume_20d=Decimal("100"),
            normal_technical_confirmation=True,
            same_bar_a_confirmation=True,
        )
    if key == "zones":
        return ZoneFacts(
            observed_at=NOW,
            source_timezone="UTC",
            selected_dates=(date(2026, 8, 24),),
            bin_count=1,
            zones=(),
        )
    if key == "divergence":
        return DivergenceFacts(observed_at=NOW, regular=(), hidden=())
    if key == "exhaustion":
        return ExhaustionFacts(observed_at=NOW, bullish=None, bearish=None)
    if key == "order_flow":
        return OrderFlowFacts(
            state=EvidenceState.AVAILABLE,
            trade_count=1,
            unknown_aggressor_count=0,
            buy_notional=Decimal("100"),
            sell_notional=Decimal("0"),
            delta_notional=Decimal("100"),
            big_trades=(),
            reversal_mig=False,
            continuation_mig=False,
            secado=False,
            ceros=False,
            telemetry_only=True,
        )
    if key == "profile":
        return ProfileFacts(
            state=EvidenceState.AVAILABLE,
            levels=(),
            point_of_control=Decimal("100"),
            value_area_low=Decimal("99"),
            value_area_high=Decimal("101"),
            total_notional=Decimal("100"),
        )
    if key == "calendar":
        return CalendarFacts(
            state=EvidenceState.AVAILABLE,
            block_new_exposure=False,
            close_intraday_positions=False,
            active_event_ids=(),
            blackout_ends_at=None,
            monday_score_penalty=0,
        )
    if key == "session":
        return SessionFacts(
            state=EvidenceState.AVAILABLE,
            session_open=True,
            entry_allowed=True,
            reduce_only=False,
            must_be_flat=False,
            overnight_allowed=False,
            entry_cutoff_at=NOW + timedelta(hours=1),
            flat_at=NOW + timedelta(hours=1, minutes=20),
            blockers=(),
        )
    if key == "costs":
        return CostFacts(
            state=EvidenceState.AVAILABLE,
            spread_per_unit=Decimal("0.1"),
            fee_per_unit=Decimal("0.02"),
            slippage_allowance_per_unit=Decimal("0.03"),
            raw_cost_per_unit=Decimal("0.15"),
            cost_offset_per_unit=Decimal("0.15"),
            round_trip_cost=Decimal("0.15"),
        )
    raise AssertionError(f"unsupported fact key {key}")


def _bundle(
    *,
    market: V6Market,
    blocked_key: str | None = None,
) -> V6EvidenceBundle:
    facts = {key: _item(key) for key in FACT_KEYS}
    if blocked_key is not None:
        facts[blocked_key] = EvidenceItem(
            state=EvidenceState.UNKNOWN,
            value=None,
            provenance=None,
            blocker_code=f"{blocked_key.upper()}_UNKNOWN",
        )
    return V6EvidenceBundle(
        market=market,
        instrument_id=new_uuid7(),
        decision_at=NOW,
        bars={},
        **facts,
    )


def _manifest() -> V6Manifest:
    return V6Manifest(
        id=new_uuid7(),
        strategy_version_id=new_uuid7(),
        source_sha256=V6_SOURCE_SHA256,
        design_sha256=V6_DESIGN_SHA256,
        configuration_hash=v6_configuration_hash(),
        registered_at=NOW - timedelta(days=1),
    )


def _context(
    bundle: V6EvidenceBundle,
    *,
    family: StrategyFamily,
    side: Side,
    indicator_count: int = 9,
    mandatory: frozenset[str] | None = None,
) -> V6RiskContext:
    direction = DIRECTION_LONG if side is Side.BUY else DIRECTION_SHORT
    keys = (
        direction,
        *(f"technical-{index:02d}" for index in range(indicator_count - 1)),
    )
    digest = bytes.fromhex(_provenance("regime").digest_sha256)
    indicators = tuple(
        MatchedIndicator(
            key=key,
            mandatory=mandatory is not None and key in mandatory,
            evidence_state=EvidenceState.AVAILABLE,
            evidence_hash=digest,
        )
        for key in sorted(keys)
    )
    is_cash = bundle.market is not V6Market.BINANCE_USDM
    return V6RiskContext(
        decision_id=new_uuid7(),
        setup_id=new_uuid7(),
        feature_snapshot_id=new_uuid7(),
        family=family,
        order_style=OrderStyle.LIMIT,
        matched_indicators=indicators,
        mandatory_indicator_codes=(
            frozenset(indicator.key for indicator in indicators)
            if mandatory is None
            else mandatory
        ),
        risk_request=V6RiskRequest(
            market=bundle.market,
            grade=SetupGrade.NORMAL,
            side=side,
            entry_price=Decimal("100"),
            structural_reference=(
                Decimal("99") if side is Side.BUY else Decimal("101")
            ),
            tick_size=Decimal("0.1"),
            spread=Decimal("0.1"),
            atr_30s=None if is_cash else Decimal("2"),
            atr_5m=Decimal("2"),
            session_start_equity=Decimal("2000"),
            current_equity=Decimal("1800"),
            daily_net_pnl=Decimal("0"),
            weekly_net_pnl=Decimal("0"),
            consecutive_net_losses=0,
            current_open_structural_risk=Decimal("0"),
            quantity_step=Decimal("0.001"),
            cost_per_unit=Decimal("0.05"),
            leverage=None if is_cash else 7,
        ),
        target_price=Decimal("103") if side is Side.BUY else Decimal("97"),
        valid_until=NOW + timedelta(minutes=5),
    )


def test_cash_is_long_only_and_never_emits_a_candidate() -> None:
    bundle = _bundle(market=V6Market.KRX_CASH)
    manifest = _manifest()

    long_decision = evaluate_v6(
        bundle,
        manifest=manifest,
        risk_context=_context(bundle, family=StrategyFamily.METODO, side=Side.BUY),
    )
    short_decision = evaluate_v6(
        bundle,
        manifest=manifest,
        risk_context=_context(
            bundle,
            family=StrategyFamily.METODO,
            side=Side.SELL,
            indicator_count=8,
        ),
    )

    assert long_decision.grade is SetupGrade.A
    assert short_decision.grade is SetupGrade.REJECT
    assert "CASH_SHORT_UNSUPPORTED" in short_decision.blockers


@pytest.mark.parametrize("side", (Side.BUY, Side.SELL))
def test_binance_emits_provider_neutral_long_and_short(side: Side) -> None:
    bundle = _bundle(market=V6Market.BINANCE_USDM)
    manifest = _manifest()

    decision = evaluate_v6(
        bundle,
        manifest=manifest,
        risk_context=_context(bundle, family=StrategyFamily.HLIT, side=side),
    )

    assert decision.grade is SetupGrade.A
    assert decision.side is side
    assert decision.calculated_quantity > 0


@pytest.mark.parametrize(
    "blocked_key",
    (
        "regime",
        "zones",
        "divergence",
        "exhaustion",
        "order_flow",
        "profile",
        "calendar",
        "session",
        "costs",
    ),
)
def test_every_binance_hard_evidence_state_blocks(blocked_key: str) -> None:
    bundle = _bundle(market=V6Market.BINANCE_USDM, blocked_key=blocked_key)
    manifest = _manifest()

    decision = evaluate_v6(
        bundle,
        manifest=manifest,
        risk_context=_context(bundle, family=StrategyFamily.HLIT, side=Side.BUY),
    )

    assert decision.grade is SetupGrade.REJECT
    assert f"{blocked_key.upper()}_UNKNOWN" in decision.blockers


def test_missing_mandatory_indicator_demotes_binance_to_normal() -> None:
    bundle = _bundle(market=V6Market.BINANCE_USDM)
    manifest = _manifest()

    decision = evaluate_v6(
        bundle,
        manifest=manifest,
        risk_context=_context(
            bundle,
            family=StrategyFamily.HLIT,
            side=Side.BUY,
            mandatory=frozenset({"missing"}),
        ),
    )

    assert decision.grade is SetupGrade.NORMAL
    assert decision.risk_fraction == Decimal("0.0025")


def test_same_bundle_and_context_produce_same_decision_digest() -> None:
    bundle = _bundle(market=V6Market.BINANCE_USDM)
    manifest = _manifest()
    context = _context(bundle, family=StrategyFamily.HLIT, side=Side.BUY)

    first = evaluate_v6(bundle, manifest=manifest, risk_context=context)
    second = evaluate_v6(bundle, manifest=manifest, risk_context=context)

    assert first == second
    assert first.decision_hash() == second.decision_hash()


def test_available_evidence_with_wrong_fact_type_fails_closed() -> None:
    bundle = _bundle(market=V6Market.BINANCE_USDM)
    bundle = V6EvidenceBundle(
        market=bundle.market,
        instrument_id=bundle.instrument_id,
        decision_at=bundle.decision_at,
        bars=bundle.bars,
        universe=bundle.universe,
        regime=EvidenceItem(
            state=EvidenceState.AVAILABLE,
            value=object(),
            provenance=_provenance("regime"),
            blocker_code=None,
        ),
        metodo=bundle.metodo,
        zones=bundle.zones,
        divergence=bundle.divergence,
        exhaustion=bundle.exhaustion,
        order_flow=bundle.order_flow,
        profile=bundle.profile,
        calendar=bundle.calendar,
        session=bundle.session,
        costs=bundle.costs,
    )
    manifest = _manifest()

    decision = evaluate_v6(
        bundle,
        manifest=manifest,
        risk_context=_context(bundle, family=StrategyFamily.HLIT, side=Side.BUY),
    )

    assert decision.grade is SetupGrade.REJECT
    assert "REGIME_VALUE_INVALID" in decision.blockers
