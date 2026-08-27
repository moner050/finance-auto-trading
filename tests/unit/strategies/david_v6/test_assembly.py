from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import OrderStyle, Side
from autotrader.persistence.mysql.repositories.david_v6_risk import (
    approved_v6_policy,
)
from autotrader.risk.models import V6RiskPolicySnapshot
from autotrader.risk.v6 import V6RiskContext, V6RiskRequest
from autotrader.shared.ids import new_uuid7
from autotrader.strategies.david_v6.assembly import (
    AssemblyInputs,
    AssemblyResult,
    AssemblySource,
    assemble_v6_evidence,
    derive_indicators,
)
from autotrader.strategies.david_v6.calendar import EventCalendar, MarketEvent
from autotrader.strategies.david_v6.costs import FeeSchedule
from autotrader.strategies.david_v6.engine import evaluate_v6
from autotrader.strategies.david_v6.grading import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
    REGULAR_HLIT_DIVERGENCE,
)
from autotrader.strategies.david_v6.manifest import (
    V6_DESIGN_SHA256,
    V6_SOURCE_SHA256,
    V6Manifest,
    v6_configuration_hash,
)
from autotrader.strategies.david_v6.models import (
    EvidenceState,
    SetupGrade,
    StrategyFamily,
    V6Market,
)
from autotrader.strategies.david_v6.order_flow import (
    OrderFlowThresholds,
    TradePrint,
)
from autotrader.strategies.david_v6.pivots import DivergenceKind
from autotrader.strategies.david_v6.regime import PessimismInputs
from autotrader.strategies.david_v6.sessions import (
    ExchangeCalendar,
    KrxMarketSafety,
    SessionKind,
)
from autotrader.strategies.david_v6.universe import evaluate_cash_universe

FIVE_MINUTES = timedelta(minutes=5)
SESSION_OPEN = datetime(2026, 8, 25, 13, 30, tzinfo=UTC)
# Enough five minute bars to clear the MACD warm-up and to cover ten days.
BAR_COUNT = 240


def _policy(market: V6Market = V6Market.US_CASH) -> V6RiskPolicySnapshot:
    """The approved policy, which is what the engine sizes against."""
    return approved_v6_policy(market, policy_version_id=new_uuid7())


def _five_minute_bars(count: int = BAR_COUNT) -> tuple[CompletedOhlcvBar, ...]:
    """A gentle saw so pivots, divergence and volume decay all have material."""
    bars: list[CompletedOhlcvBar] = []
    start = SESSION_OPEN - FIVE_MINUTES * count
    for index in range(count):
        base = Decimal(100) + Decimal(index % 7)
        bars.append(
            CompletedOhlcvBar(
                timestamp=start + FIVE_MINUTES * index,
                open=base,
                high=base + Decimal(2),
                low=base - Decimal(1),
                close=base + Decimal(1),
                volume=Decimal(1000 - index),
            )
        )
    return tuple(bars)


def _daily_bars(count: int = 210) -> tuple[CompletedOhlcvBar, ...]:
    start = SESSION_OPEN - timedelta(days=count + 1)
    return tuple(
        CompletedOhlcvBar(
            timestamp=start + timedelta(days=index),
            open=Decimal(100 + index),
            high=Decimal(102 + index),
            low=Decimal(99 + index),
            close=Decimal(101 + index),
            volume=Decimal(500),
        )
        for index in range(count)
    )


def _calendar(kind: SessionKind) -> ExchangeCalendar:
    return ExchangeCalendar(
        session_date=date(2026, 8, 25),
        kind=kind,
        source_timezone="UTC",
        is_trading_day=True,
        session_open_at=SESSION_OPEN - timedelta(hours=6),
        session_close_at=SESSION_OPEN + timedelta(hours=6),
        close_auction_at=(
            SESSION_OPEN + timedelta(hours=5) if kind is SessionKind.KRX_HLIT else None
        ),
        pre_open_at=None,
        captured_at=SESSION_OPEN - timedelta(days=1),
        valid_until=SESSION_OPEN + timedelta(hours=12),
    )


def _quiet_calendar(*events: MarketEvent) -> EventCalendar:
    """A calendar that was fetched, whatever it happened to contain."""
    return EventCalendar(
        captured_at=SESSION_OPEN - timedelta(hours=12),
        valid_until=SESSION_OPEN + timedelta(hours=12),
        events=events,
    )


def _trades() -> tuple[TradePrint, ...]:
    return tuple(
        TradePrint(
            provider_trade_id=f"t{index}",
            occurred_at=SESSION_OPEN - timedelta(minutes=10) + timedelta(seconds=index),
            price=Decimal(100) + Decimal(index % 3),
            quantity=Decimal(1),
            buyer_maker=index % 2 == 0,
        )
        for index in range(60)
    )


def _thresholds() -> OrderFlowThresholds:
    return OrderFlowThresholds(
        tick_size=Decimal("0.1"),
        normal_big_trade_notional=Decimal("1000"),
        extreme_big_trade_notional=Decimal("5000"),
        delta_p90_notional=Decimal("500"),
        atr_30s=Decimal("1"),
        ceros_near_zero_notional=Decimal("10"),
        ceros_large_notional=Decimal("100"),
    )


def _source() -> AssemblySource:
    return AssemblySource(
        name="TEST_PROVIDER",
        timezone="UTC",
        captured_at=SESSION_OPEN,
    )


def _inputs(market: V6Market, **changes: object) -> AssemblyInputs:
    is_binance = market is V6Market.BINANCE_USDM
    values: dict[str, object] = {
        "market": market,
        "instrument_id": new_uuid7(),
        "decision_at": SESSION_OPEN,
        "source": _source(),
        "bars": {"5m": _five_minute_bars(), "1d": _daily_bars()},
        "calendar": _calendar(
            SessionKind.BINANCE_USDM if is_binance else SessionKind.US_HLIT
        ),
        "events": _quiet_calendar(),
        "universe": (
            None
            if is_binance
            else evaluate_cash_universe(
                country_strength_confirmed=True,
                member_as_of=True,
                common_stock_as_of=True,
                median_value_20d=Decimal("100"),
                cross_section_median_value_20d=Decimal("50"),
                sector_return_70d_rank=1,
                sector_classification="technology",
            )
        ),
        "benchmark_returns": tuple(Decimal("0.001") for _ in range(210)),
        "atr_ratio": Decimal("0.5"),
        "range_efficiency": Decimal("0.5"),
        "pessimism": PessimismInputs(
            completed_date=date(2026, 8, 24),
            volatility_percentile=Decimal("0.5"),
            put_call_percentile=Decimal("0.5"),
            breadth_percentile=Decimal("0.5"),
        ),
        "trades": _trades() if is_binance else None,
        "order_flow_thresholds": _thresholds() if is_binance else None,
        "fee_schedule": FeeSchedule(
            entry_fee_per_unit=Decimal("0.02"),
            exit_taker_fee_per_unit=Decimal("0.03"),
        ),
        "spread": Decimal("0.1"),
        "quantity": Decimal(1),
        "stop_slippage_q95": Decimal("0.05"),
        "tick_size": Decimal("0.1"),
    }
    values.update(changes)
    return AssemblyInputs(**values)  # type: ignore[arg-type]


def test_a_complete_capture_produces_every_fact() -> None:
    result = assemble_v6_evidence(_inputs(V6Market.US_CASH))

    bundle = result.bundle
    for name in ("universe", "regime", "zones", "divergence", "exhaustion"):
        item = getattr(bundle, name)
        assert item.state is EvidenceState.AVAILABLE, f"{name} was {item.blocker_code}"
    assert bundle.calendar.state is EvidenceState.AVAILABLE
    assert bundle.session.state is EvidenceState.AVAILABLE
    assert bundle.costs.state is EvidenceState.AVAILABLE


def test_every_available_fact_carries_provenance() -> None:
    bundle = assemble_v6_evidence(_inputs(V6Market.US_CASH)).bundle

    items = (
        bundle.universe,
        bundle.regime,
        bundle.zones,
        bundle.divergence,
        bundle.exhaustion,
        bundle.calendar,
        bundle.session,
        bundle.costs,
    )
    for item in items:
        if item.state is not EvidenceState.AVAILABLE:
            continue
        assert item.provenance is not None
        assert item.provenance.source == "TEST_PROVIDER"
        assert len(item.provenance.digest_sha256) == 64
        assert item.provenance.observed_at <= item.provenance.captured_at


def test_missing_bars_never_fabricate_a_fact() -> None:
    result = assemble_v6_evidence(_inputs(V6Market.US_CASH, bars={}))

    bundle = result.bundle
    assert bundle.zones.state is not EvidenceState.AVAILABLE
    assert bundle.zones.value is None
    assert bundle.zones.blocker_code == "ZONES_BARS_UNAVAILABLE"
    assert bundle.divergence.blocker_code == "DIVERGENCE_MACD_WARMUP_UNAVAILABLE"
    assert bundle.exhaustion.blocker_code == "EXHAUSTION_INPUTS_UNAVAILABLE"
    assert result.hlit is None


def test_missing_optional_inputs_each_name_their_own_blocker() -> None:
    bundle = assemble_v6_evidence(
        _inputs(
            V6Market.US_CASH,
            universe=None,
            events=None,
            calendar=None,
            fee_schedule=None,
            benchmark_returns=None,
        )
    ).bundle

    assert bundle.universe.blocker_code == "UNIVERSE_UNAVAILABLE"
    assert bundle.calendar.blocker_code == "CALENDAR_UNAVAILABLE"
    assert bundle.session.blocker_code == "SESSION_CALENDAR_UNAVAILABLE"
    assert bundle.costs.blocker_code == "COSTS_UNAVAILABLE"
    assert bundle.regime.blocker_code == "REGIME_UNAVAILABLE"


def test_a_forming_bar_is_dropped_rather_than_failing_the_capture() -> None:
    bars = _five_minute_bars()
    forming = CompletedOhlcvBar(
        timestamp=SESSION_OPEN,
        open=Decimal(100),
        high=Decimal(101),
        low=Decimal(99),
        close=Decimal(100),
        volume=Decimal(10),
    )

    result = assemble_v6_evidence(
        _inputs(V6Market.US_CASH, bars={"5m": (*bars, forming)})
    )

    assert result.bundle.zones.state is EvidenceState.AVAILABLE


def test_cash_markets_mark_order_flow_and_profile_not_applicable() -> None:
    bundle = assemble_v6_evidence(_inputs(V6Market.US_CASH)).bundle

    assert bundle.order_flow.state is EvidenceState.NOT_APPLICABLE
    assert bundle.order_flow.blocker_code == "ORDER_FLOW_BINANCE_ONLY"
    assert bundle.profile.state is EvidenceState.NOT_APPLICABLE
    assert bundle.metodo.state is EvidenceState.AVAILABLE


def test_binance_marks_universe_and_metodo_not_applicable() -> None:
    bundle = assemble_v6_evidence(_inputs(V6Market.BINANCE_USDM)).bundle

    assert bundle.universe.state is EvidenceState.NOT_APPLICABLE
    assert bundle.metodo.state is EvidenceState.NOT_APPLICABLE
    assert bundle.order_flow.state is EvidenceState.AVAILABLE
    assert bundle.profile.state is EvidenceState.AVAILABLE


def test_a_calendar_for_another_market_is_refused() -> None:
    bundle = assemble_v6_evidence(
        _inputs(V6Market.US_CASH, calendar=_calendar(SessionKind.KRX_HLIT))
    ).bundle

    assert bundle.session.blocker_code == "SESSION_CALENDAR_MARKET_MISMATCH"


def test_krx_without_market_safety_blocks_the_session() -> None:
    bundle = assemble_v6_evidence(
        _inputs(
            V6Market.KRX_CASH,
            calendar=_calendar(SessionKind.KRX_HLIT),
            bars={"5m": _five_minute_bars(), "1d": _daily_bars()},
        )
    ).bundle

    assert bundle.session.state is EvidenceState.AVAILABLE
    session = bundle.session.value
    assert session is not None
    assert "KRX_MARKET_SAFETY_UNAVAILABLE" in session.blockers  # type: ignore[attr-defined]


def test_krx_market_safety_reaches_the_session_fact() -> None:
    bundle = assemble_v6_evidence(
        _inputs(
            V6Market.KRX_CASH,
            calendar=_calendar(SessionKind.KRX_HLIT),
            market_safety=KrxMarketSafety(
                observed_at=SESSION_OPEN,
                has_active_krx_vi=True,
                is_single_price_auction=False,
            ),
        )
    ).bundle

    session = bundle.session.value
    assert session is not None
    assert "KRX_VI_ACTIVE" in session.blockers  # type: ignore[attr-defined]


def test_a_news_blackout_reaches_the_calendar_fact() -> None:
    event = MarketEvent(
        event_id="fomc",
        source_key="test",
        scheduled_at=SESSION_OPEN + timedelta(minutes=5),
        impact_stars=3,
        strong_surprise=None,
        is_nfp=False,
        session_close_at=None,
    )

    bundle = assemble_v6_evidence(
        _inputs(V6Market.US_CASH, events=_quiet_calendar(event))
    ).bundle

    calendar = bundle.calendar.value
    assert calendar is not None
    assert calendar.block_new_exposure is True  # type: ignore[attr-defined]


def test_inputs_reject_a_non_v7_instrument_id() -> None:
    from uuid import UUID

    with pytest.raises(ValueError, match="UUIDv7"):
        _inputs(V6Market.US_CASH, instrument_id=UUID(int=0))


def _decelerating_decline_bars() -> tuple[CompletedOhlcvBar, ...]:
    """Bars that actually produce the setup the specification describes.

    A baseline oscillation builds MACD history, a rally prints the swing high
    that becomes anchor A, and then a decelerating decline on falling volume
    prints marginal new lows while momentum turns up. That is a regular
    bullish divergence and an exhaustion sequence at the same time.
    """
    closes: list[Decimal] = [
        Decimal(120) + Decimal((index * 7) % 11) - Decimal(5) for index in range(700)
    ]
    closes += [Decimal(120) + Decimal(index) * Decimal("0.5") for index in range(40)]
    price = closes[-1]
    for step in ("6", "4.5", "3.2", "2.2", "1.4", "0.9", "0.5", "0.3", "0.2", "0.1"):
        for _ in range(4):
            price -= Decimal(step) / Decimal(4)
            closes.append(price)
    start = SESSION_OPEN - FIVE_MINUTES * len(closes)
    bars: list[CompletedOhlcvBar] = []
    for index, close in enumerate(closes):
        volume = (
            Decimal(5000) - Decimal(index)
            if index < 740
            else Decimal(2000) - Decimal((index - 740) * 30)
        )
        bars.append(
            CompletedOhlcvBar(
                timestamp=start + FIVE_MINUTES * index,
                open=close,
                high=close + Decimal("0.5"),
                low=close - Decimal("0.5"),
                close=close,
                volume=max(volume, Decimal(1)),
            )
        )
    return tuple(bars)


def _setup_inputs() -> AssemblyInputs:
    return _inputs(
        V6Market.US_CASH,
        bars={"5m": _decelerating_decline_bars(), "1d": _daily_bars()},
        at_observed_all_time_high=True,
    )


def test_real_bars_produce_a_regular_divergence() -> None:
    bundle = assemble_v6_evidence(_setup_inputs()).bundle

    assert bundle.divergence.state is EvidenceState.AVAILABLE
    divergence = bundle.divergence.value
    assert divergence is not None
    kinds = {signal.kind for signal in divergence.regular}  # type: ignore[attr-defined]
    assert DivergenceKind.REGULAR_BULLISH in kinds


def test_real_bars_draw_the_hlit_levels() -> None:
    result = assemble_v6_evidence(_setup_inputs())

    assert result.hlit is not None
    setup = result.hlit.bullish
    assert setup is not None
    assert setup.anchor_a > setup.anchor_b
    # Section 3: the levels retrace upward from the low anchor toward 66%.
    assert setup.anchor_b < setup.fib_25 < setup.fib_50 < setup.fib_66
    assert setup.target_price == setup.fib_66
    span = setup.anchor_a - setup.anchor_b
    assert setup.fib_66 == setup.anchor_b + span * Decimal("0.66")


def test_real_bars_confirm_the_exhaustion_sequence() -> None:
    bundle = assemble_v6_evidence(_setup_inputs()).bundle

    assert bundle.zones.state is EvidenceState.AVAILABLE
    zones = bundle.zones.value
    assert zones is not None
    assert len(zones.zones) > 0  # type: ignore[attr-defined]
    exhaustion = bundle.exhaustion.value
    assert exhaustion is not None
    sequence = exhaustion.bullish  # type: ignore[attr-defined]
    assert sequence is not None
    assert sequence.confirmed is True
    assert len(sequence.history) >= 3


def _risk_context(result: AssemblyResult, side: Side) -> V6RiskContext:
    """Build the account-side context the loop will own, from the drawn setup."""
    assert result.hlit is not None
    setup = result.hlit.for_side(side)
    assert setup is not None
    exhaustion = result.bundle.exhaustion.value
    assert exhaustion is not None
    sequence = exhaustion.bullish if side is Side.BUY else exhaustion.bearish  # type: ignore[attr-defined]
    assert sequence is not None
    entry = setup.anchor_b + (setup.fib_25 - setup.anchor_b) / Decimal(2)
    return V6RiskContext(
        decision_id=new_uuid7(),
        setup_id=new_uuid7(),
        feature_snapshot_id=new_uuid7(),
        family=StrategyFamily.HLIT,
        order_style=OrderStyle.LIMIT,
        matched_indicators=derive_indicators(result, side=side, reference_price=entry),
        mandatory_indicator_codes=frozenset(),
        # The policy is for the market the request names, which the
        # context refuses to let drift apart.
        policy=_policy(result.bundle.market),
        risk_request=V6RiskRequest(
            market=result.bundle.market,
            grade=SetupGrade.NORMAL,
            side=side,
            entry_price=entry,
            structural_reference=sequence.structural_reference_price,
            tick_size=Decimal("0.01"),
            spread=Decimal("0.01"),
            atr_30s=None,
            atr_5m=Decimal("0.5"),
            session_start_equity=Decimal("100000"),
            current_equity=Decimal("100000"),
            daily_net_pnl=Decimal(0),
            weekly_net_pnl=Decimal(0),
            consecutive_net_losses=0,
            current_open_structural_risk=Decimal(0),
            quantity_step=Decimal(1),
            cost_per_unit=Decimal("0.05"),
            leverage=None,
        ),
        target_price=setup.target_price,
        valid_until=SESSION_OPEN + timedelta(minutes=5),
    )


def _manifest() -> V6Manifest:
    return V6Manifest(
        id=new_uuid7(),
        strategy_version_id=new_uuid7(),
        source_sha256=V6_SOURCE_SHA256,
        design_sha256=V6_DESIGN_SHA256,
        configuration_hash=v6_configuration_hash(),
        registered_at=SESSION_OPEN - timedelta(days=1),
    )


def test_derived_indicators_prove_the_direction_from_the_divergence() -> None:
    result = assemble_v6_evidence(_setup_inputs())

    indicators = derive_indicators(
        result, side=Side.BUY, reference_price=Decimal("120")
    )

    keys = {indicator.key for indicator in indicators}
    assert DIRECTION_LONG in keys
    assert REGULAR_HLIT_DIVERGENCE in keys


def test_without_a_regular_divergence_no_direction_is_derived() -> None:
    """The direction claim is only ever backed by a regular divergence."""
    result = assemble_v6_evidence(_inputs(V6Market.US_CASH))
    divergence = result.bundle.divergence.value
    assert divergence is not None
    assert divergence.regular == ()  # type: ignore[attr-defined]

    for side in (Side.BUY, Side.SELL):
        keys = {
            indicator.key
            for indicator in derive_indicators(
                result, side=side, reference_price=Decimal("120")
            )
        }
        assert DIRECTION_LONG not in keys
        assert DIRECTION_SHORT not in keys
        assert REGULAR_HLIT_DIVERGENCE not in keys


def test_every_derived_indicator_is_backed_by_bundle_provenance() -> None:
    result = assemble_v6_evidence(_setup_inputs())
    known = {
        bytes.fromhex(item.provenance.digest_sha256)
        for item in (
            result.bundle.divergence,
            result.bundle.profile,
            result.bundle.order_flow,
            result.bundle.calendar,
        )
        if item.provenance is not None
    }

    indicators = derive_indicators(
        result, side=Side.BUY, reference_price=Decimal("120")
    )

    assert indicators
    for indicator in indicators:
        assert indicator.evidence_hash in known


def test_the_assembled_bundle_reaches_a_decision() -> None:
    """The whole point of the assembler: bars in, a graded decision out."""
    result = assemble_v6_evidence(_setup_inputs())

    decision = evaluate_v6(
        result.bundle,
        manifest=_manifest(),
        risk_context=_risk_context(result, Side.BUY),
    )

    assert decision.market is V6Market.US_CASH
    assert "REGULAR_DIVERGENCE_ABSENT" not in decision.blockers
    assert "EXHAUSTION_ABSENT" not in decision.blockers
    assert "NO_MARKED_ZONE" not in decision.blockers
    assert "DIRECTION_EVIDENCE_MISSING" not in decision.blockers
