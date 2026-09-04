"""Assemble a V6 evidence bundle from completed market data.

This is the link between market data and the decision engine. Each fact is
produced by its own evaluator and wrapped with the provenance of the input it
came from. A missing or unusable input never becomes a fabricated value: the
fact stays unavailable and carries a blocker code, which the engine then
refuses to trade on.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import Side
from autotrader.shared.decimal import require_decimal
from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.calendar import (
    CalendarFacts,
    EventCalendar,
    evaluate_event_window,
)
from autotrader.strategies.david_v6.costs import FeeSchedule, estimate_round_trip_cost
from autotrader.strategies.david_v6.direction import aligned_macd_histogram
from autotrader.strategies.david_v6.evidence import (
    TIMEFRAMES,
    EvidenceItem,
    EvidenceProvenance,
    V6EvidenceBundle,
)
from autotrader.strategies.david_v6.exhaustion import evaluate_exhaustion
from autotrader.strategies.david_v6.grading import (
    BLOCKING_BIG_TRADE_AHEAD,
    DIRECTION_LONG,
    DIRECTION_SHORT,
    HIDDEN_DIVERGENCE,
    HIGH_IMPACT_NEWS_RISK,
    HIGHER_TIMEFRAME_BIAS,
    PROFILE_VALUE_CONFLUENCE,
    REGULAR_HLIT_DIVERGENCE,
    SUPPORTING_BIG_TRADE_BEHIND,
    V1_MIG_REVERSAL,
    V1_SECADO,
)
from autotrader.strategies.david_v6.hlit import HlitFacts, build_hlit_setups
from autotrader.strategies.david_v6.metodo import (
    evaluate_metodo,
)
from autotrader.strategies.david_v6.models import (
    EvidenceState,
    MatchedIndicator,
    V6Market,
)
from autotrader.strategies.david_v6.order_flow import (
    BIG_TRADE_WINDOW,
    OrderFlowFacts,
    OrderFlowThresholds,
    TradePrint,
    aggregate_order_flow,
    blocking_big_trade_ahead,
)
from autotrader.strategies.david_v6.pivots import (
    DivergenceFacts,
    DivergenceKind,
    Pivot,
    PivotConfig,
    confirmed_pivots,
    evaluate_divergence,
)
from autotrader.strategies.david_v6.profile import ProfileFacts, build_profile
from autotrader.strategies.david_v6.regime import (
    PessimismInputs,
    RegimeFacts,
    RegimeLabel,
    evaluate_regime,
)
from autotrader.strategies.david_v6.sessions import (
    ExchangeCalendar,
    KrxMarketSafety,
    SessionKind,
    evaluate_session,
)
from autotrader.strategies.david_v6.universe import UniverseFacts
from autotrader.strategies.david_v6.zones import (
    ZoneConfig,
    ZoneFacts,
    build_hlit_zones,
)

# Section 4.2 works the five minute macro, and zone construction requires
# contiguous five minute bars.
HLIT_TIMEFRAME_KEY = "5m"
_DAILY_TIMEFRAME_KEY = "1d"
# Section 4.2 confirms exhaustion here, not on the five-minute macro read.
_EXECUTION_TIMEFRAME_KEY = "30s"
# Owned by `order_flow`, which derives the marker cap from its ratio to
# the liquidity session. Two copies of it would let the cap drift from
# the window it is a rate over.
_ORDER_FLOW_WINDOW = BIG_TRADE_WINDOW
_MARKET_SESSION_KINDS: Mapping[V6Market, SessionKind] = {
    V6Market.KRX_CASH: SessionKind.KRX_HLIT,
    V6Market.US_CASH: SessionKind.US_HLIT,
    V6Market.BINANCE_USDM: SessionKind.BINANCE_USDM,
}


def _no_bars() -> dict[str, tuple[CompletedOhlcvBar, ...]]:
    return {}


@dataclass(frozen=True, slots=True)
class AssemblySource:
    """Where the evidence came from, recorded on every fact."""

    name: str
    timezone: str
    captured_at: datetime

    def __post_init__(self) -> None:
        for name in ("name", "timezone"):
            value = getattr(self, name)
            if type(value) is not str or not value or value.strip() != value:
                raise ValueError(f"source {name} must be non-empty trimmed text")
        if type(self.captured_at) is not datetime:
            raise TypeError("captured_at must be an exact datetime")
        object.__setattr__(self, "captured_at", require_utc(self.captured_at))


@dataclass(frozen=True, slots=True)
class AssemblyInputs:
    """Market data for one decision.

    Every optional field that is absent yields an unavailable fact rather than
    a guess, so an incomplete capture can never produce a tradeable setup.
    """

    market: V6Market
    instrument_id: UUID
    decision_at: datetime
    source: AssemblySource
    bars: Mapping[str, tuple[CompletedOhlcvBar, ...]] = field(default_factory=_no_bars)
    calendar: ExchangeCalendar | None = None
    market_safety: KrxMarketSafety | None = None
    events: EventCalendar | None = None
    universe: UniverseFacts | None = None
    benchmark_returns: tuple[Decimal, ...] | None = None
    atr_ratio: Decimal | None = None
    range_efficiency: Decimal | None = None
    pessimism: PessimismInputs | None = None
    trades: tuple[TradePrint, ...] | None = None
    order_flow_thresholds: OrderFlowThresholds | None = None
    fee_schedule: FeeSchedule | None = None
    spread: Decimal | None = None
    quantity: Decimal | None = None
    stop_slippage_q95: Decimal | None = None
    tick_size: Decimal | None = None

    def __post_init__(self) -> None:
        if type(cast(object, self.market)) is not V6Market:
            raise TypeError("market must be an exact V6Market")
        instrument_id = cast(object, self.instrument_id)
        if not isinstance(instrument_id, UUID) or instrument_id.version != 7:
            raise ValueError("instrument_id must be UUIDv7")
        if type(self.decision_at) is not datetime:
            raise TypeError("decision_at must be an exact datetime")
        object.__setattr__(self, "decision_at", require_utc(self.decision_at))
        if type(cast(object, self.source)) is not AssemblySource:
            raise TypeError("source must be an exact AssemblySource")


@dataclass(frozen=True, slots=True)
class AssemblyResult:
    """The bundle plus the drawn HLIT levels, which the bundle cannot carry."""

    bundle: V6EvidenceBundle
    hlit: HlitFacts | None


def assemble_v6_evidence(inputs: AssemblyInputs) -> AssemblyResult:
    if type(inputs) is not AssemblyInputs:
        raise TypeError("inputs must be exact AssemblyInputs")
    inputs.__post_init__()
    decision_at = inputs.decision_at
    hlit_bars = _completed_bars(inputs, HLIT_TIMEFRAME_KEY)
    daily_bars = _completed_bars(inputs, _DAILY_TIMEFRAME_KEY)

    zones = _zones(inputs, hlit_bars)
    divergence, pivots, macd_bars = _divergence(inputs, hlit_bars)
    execution_bars, execution_pivots, execution_key = _execution_scale(
        inputs, macd_bars, pivots
    )
    hlit = (
        build_hlit_setups(macd_bars, cast(DivergenceFacts, divergence.value))
        if divergence.state is EvidenceState.AVAILABLE
        else None
    )
    return AssemblyResult(
        bundle=V6EvidenceBundle(
            market=inputs.market,
            instrument_id=inputs.instrument_id,
            decision_at=decision_at,
            bars=_bar_items(inputs),
            universe=_universe(inputs),
            regime=_regime(inputs),
            metodo=cast(
                EvidenceItem[object],
                evaluate_metodo(
                    market=inputs.market,
                    daily_bars=daily_bars or (),
                    decision_at=decision_at,
                ),
            ),
            zones=zones,
            divergence=divergence,
            exhaustion=_exhaustion(
                inputs, execution_bars, zones, execution_pivots, execution_key
            ),
            order_flow=_order_flow(inputs),
            profile=_profile(inputs),
            calendar=_calendar(inputs),
            session=_session(inputs),
            costs=_costs(inputs),
        ),
        hlit=hlit,
    )


def derive_indicators(
    result: AssemblyResult,
    *,
    side: Side,
    reference_price: Decimal,
) -> tuple[MatchedIndicator, ...]:
    """Turn assembled facts into the scored indicators of section 21.3.

    Each indicator carries the provenance digest of the fact that proves it,
    which is what lets the engine reject a claim the bundle does not support.
    """
    if type(result) is not AssemblyResult:
        raise TypeError("result must be an exact AssemblyResult")
    if type(side) is not Side:
        raise TypeError("side must be an exact Side")
    price = require_decimal(reference_price)
    if price <= 0:
        raise ValueError("reference_price must be positive")
    bundle = result.bundle
    matched: list[MatchedIndicator] = []

    divergence_hash = _digest_bytes(bundle.divergence)
    if divergence_hash is not None:
        facts = cast(DivergenceFacts, bundle.divergence.value)
        wanted = (
            DivergenceKind.REGULAR_BULLISH
            if side is Side.BUY
            else DivergenceKind.REGULAR_BEARISH
        )
        if any(signal.kind is wanted for signal in facts.regular):
            matched.append(_indicator(REGULAR_HLIT_DIVERGENCE, divergence_hash))
            matched.append(
                _indicator(
                    DIRECTION_LONG if side is Side.BUY else DIRECTION_SHORT,
                    divergence_hash,
                )
            )
        if facts.hidden:
            matched.append(_indicator(HIDDEN_DIVERGENCE, divergence_hash))

    profile_hash = _digest_bytes(bundle.profile)
    if profile_hash is not None:
        profile = cast(ProfileFacts, bundle.profile.value)
        if profile.point_of_control is not None:
            matched.append(_indicator(PROFILE_VALUE_CONFLUENCE, profile_hash))

    # Section 21.2 puts this in `mandatory` and section 21.3 gives it +2, and
    # the fact it needs has been computed on every pass all along: the regime
    # is section 2.1's SMA 6/70/200 over daily closes, which is the daily bias
    # the phase plan calls a higher-timeframe veto. Only `excluded` was ever
    # read off it.
    regime_hash = _digest_bytes(bundle.regime)
    if regime_hash is not None:
        regime = cast(RegimeFacts, bundle.regime.value)
        aligned = RegimeLabel.TREND_UP if side is Side.BUY else RegimeLabel.TREND_DOWN
        # BALANCE is not a bias to agree with. Reading it as agreement would
        # score every sideways day as confirmation of both directions.
        if regime.trend is aligned:
            matched.append(_indicator(HIGHER_TIMEFRAME_BIAS, regime_hash))

    order_flow_hash = _digest_bytes(bundle.order_flow)
    if order_flow_hash is not None:
        flow = cast(OrderFlowFacts, bundle.order_flow.value)
        # An unmeasured window scores neither way. The engine turns it into a
        # blocker; scoring it as though no obstacle was found would credit the
        # setup for a look that never happened.
        if flow.big_trades is not None:
            if blocking_big_trade_ahead(flow, side=side, reference_price=price):
                matched.append(_indicator(BLOCKING_BIG_TRADE_AHEAD, order_flow_hash))
            elif flow.big_trades:
                matched.append(_indicator(SUPPORTING_BIG_TRADE_BEHIND, order_flow_hash))
        # `aggregate_order_flow` has computed these on every pass since it was
        # written and nothing read them, so §21.3's two-point weights could
        # never be earned and the candidate threshold of seven was out of
        # reach by arithmetic. Section 15.2 puts both at `score_only`: they may
        # carry weight and may not be required, which is what an entry in the
        # matched list is.
        #
        # `is True` rather than truthiness because the field has three states -
        # None is a window that could not measure, and only True is a match.
        if flow.secado is True:
            matched.append(_indicator(V1_SECADO, order_flow_hash))
        if flow.reversal_mig is True:
            matched.append(_indicator(V1_MIG_REVERSAL, order_flow_hash))
        # Not wired, and each for its own reason. `continuation_mig` carries no
        # weight in §21.3. `ceros` is `telemetry_only` in §15.2 at LOW
        # confidence, and `_TELEMETRY_ONLY` already forces its weight to zero -
        # scoring it would add a row that says nothing.

    calendar_hash = _digest_bytes(bundle.calendar)
    if calendar_hash is not None:
        calendar = cast(CalendarFacts, bundle.calendar.value)
        if calendar.block_new_exposure:
            matched.append(_indicator(HIGH_IMPACT_NEWS_RISK, calendar_hash))

    return tuple(sorted(matched, key=lambda indicator: indicator.key))


# `_extension_cluster` lived here and produced FIBONACCI_EXTENSION_CLUSTER
# when an extension fell inside a marked zone or the value area. It fired on
# three of three setups, and measuring the geometry says why: twenty zones
# cover 96% of the price range they span, so "inside a zone" is very nearly
# always true. The predicate was not strict or loose, it was vacuous, and a
# +2 that every setup earns is the candidate threshold quietly lowered by two.
#
# Removed rather than replaced. Containment fails because the interior of a
# tiled range means nothing; the meaningful question is whether an extension
# lands on a zone *boundary*, which is a price the market turned at. That
# needs a tolerance, section 15.2's instruction for a number nobody published
# is to expose it and run a sensitivity analysis, and choosing one here by
# how plausible the hit rate looks would be fitting the thing under test.
#
# `extension_prices` stays. Section 21.4's 1.272 and 1.618 are the documented
# targets and section 15.2 rates their values HIGH; what was wrong was the use
# made of them here, not the levels.


def _indicator(key: str, evidence_hash: bytes) -> MatchedIndicator:
    return MatchedIndicator(
        key=key,
        mandatory=False,
        evidence_state=EvidenceState.AVAILABLE,
        evidence_hash=evidence_hash,
    )


def _digest_bytes(item: EvidenceItem[object]) -> bytes | None:
    if item.state is not EvidenceState.AVAILABLE or item.provenance is None:
        return None
    return bytes.fromhex(item.provenance.digest_sha256)


def _bar_items(
    inputs: AssemblyInputs,
) -> dict[str, EvidenceItem[tuple[CompletedOhlcvBar, ...]]]:
    items: dict[str, EvidenceItem[tuple[CompletedOhlcvBar, ...]]] = {}
    for key in inputs.bars:
        values = _drop_forming(inputs, key)
        if not values:
            items[key] = cast(
                EvidenceItem[tuple[CompletedOhlcvBar, ...]],
                _unavailable(f"BARS_{key.upper()}_UNAVAILABLE"),
            )
            continue
        items[key] = EvidenceItem(
            state=EvidenceState.AVAILABLE,
            value=values,
            provenance=_provenance(
                inputs,
                key=f"bars:{key}",
                observed_at=values[-1].timestamp,
                payload=_bars_payload(values),
            ),
            blocker_code=None,
        )
    return items


def _completed_bars(
    inputs: AssemblyInputs,
    key: str,
) -> tuple[CompletedOhlcvBar, ...] | None:
    completed = _drop_forming(inputs, key)
    return completed or None


def _drop_forming(
    inputs: AssemblyInputs,
    key: str,
) -> tuple[CompletedOhlcvBar, ...]:
    """Discard a bar that has not closed yet.

    The bundle refuses a forming bar outright. A capture that ran a moment
    early is ordinary, so drop the bar rather than fail the whole assembly.
    """
    timeframe = TIMEFRAMES.get(key)
    if timeframe is None:
        return ()
    return tuple(
        bar
        for bar in inputs.bars.get(key, ())
        if bar.timestamp + timeframe <= inputs.decision_at
    )


def _universe(inputs: AssemblyInputs) -> EvidenceItem[object]:
    if inputs.market is V6Market.BINANCE_USDM:
        return EvidenceItem(
            state=EvidenceState.NOT_APPLICABLE,
            value=None,
            provenance=None,
            blocker_code="UNIVERSE_CASH_ONLY",
        )
    if inputs.universe is None:
        return _unavailable("UNIVERSE_UNAVAILABLE")
    return _available(
        inputs,
        key="universe",
        value=inputs.universe,
        observed_at=inputs.source.captured_at,
        payload={"eligible": inputs.universe.eligible},
    )


def _regime(inputs: AssemblyInputs) -> EvidenceItem[object]:
    if inputs.benchmark_returns is None:
        # The author's rule is SMA 6/70/200 on the instrument, so the closes
        # are the only thing it cannot be evaluated without. The other three
        # are observations beside the rule and a condition on one signal.
        return _unavailable("REGIME_UNAVAILABLE")
    facts = evaluate_regime(
        benchmark_returns=inputs.benchmark_returns,
        atr_ratio=inputs.atr_ratio,
        range_efficiency=inputs.range_efficiency,
        pessimism_inputs=inputs.pessimism,
    )
    return _available(
        inputs,
        key="regime",
        value=facts,
        observed_at=inputs.source.captured_at,
        payload={"returns": len(inputs.benchmark_returns)},
    )


def _zones(
    inputs: AssemblyInputs,
    bars: tuple[CompletedOhlcvBar, ...] | None,
) -> EvidenceItem[object]:
    if bars is None:
        return _unavailable("ZONES_BARS_UNAVAILABLE")
    try:
        facts = build_hlit_zones(
            bars, ZoneConfig(source_timezone=inputs.source.timezone)
        )
    except ValueError:
        return _unavailable("ZONES_EVIDENCE_INVALID")
    return _available(
        inputs,
        key="zones",
        value=facts,
        observed_at=facts.observed_at,
        payload={"zones": len(facts.zones)},
    )


def _divergence(
    inputs: AssemblyInputs,
    bars: tuple[CompletedOhlcvBar, ...] | None,
) -> tuple[EvidenceItem[object], tuple[Pivot, ...], tuple[CompletedOhlcvBar, ...]]:
    if bars is None:
        return _unavailable("DIVERGENCE_MACD_WARMUP_UNAVAILABLE"), (), ()
    # Shared with the source that picks the side, so the two cannot align the
    # warm-up differently and disagree about which divergence is there.
    aligned = aligned_macd_histogram(bars)
    if aligned is None:
        return _unavailable("DIVERGENCE_MACD_WARMUP_UNAVAILABLE"), (), ()
    aligned_bars, histogram = aligned
    facts = evaluate_divergence(aligned_bars, histogram)
    pivots = confirmed_pivots(aligned_bars, PivotConfig())
    item = _available(
        inputs,
        key="divergence",
        value=facts,
        observed_at=aligned_bars[-1].timestamp,
        payload={"regular": len(facts.regular), "hidden": len(facts.hidden)},
    )
    return item, pivots, aligned_bars


def _execution_scale(
    inputs: AssemblyInputs,
    hlit_bars: tuple[CompletedOhlcvBar, ...],
    hlit_pivots: tuple[Pivot, ...],
) -> tuple[tuple[CompletedOhlcvBar, ...], tuple[Pivot, ...], str]:
    """The series exhaustion is read on, and which one it turned out to be.

    Section 4.2 (A0) walks down rather than staying put: five minutes for the
    macro read, then "1분 + 30초 분할 화면으로 하강", and the volume divergence
    is confirmed at thirty seconds because there are twice as many candles to
    see it in. Everything before this line stays on five minutes - the
    divergence, the anchor, the zones - because that is where section 14.1
    puts them in the causal order.

    The five-minute series is the fallback rather than the choice. Cash
    bundles cannot carry thirty-second evidence at all (`evidence.py` refuses
    it), and on Binance the tape can be thin after a restart, so a bundle
    without the series still assembles instead of losing its exhaustion
    reading entirely.

    Which one was used is returned rather than inferred, because a filter
    that silently changes what it reads is one the recorded evidence cannot
    be read back against. The plan's section 33 measured a 132-to-1
    difference between the scales; a bundle has to say which side of that it
    is on.
    """
    bars = _completed_bars(inputs, _EXECUTION_TIMEFRAME_KEY)
    if bars is None:
        return hlit_bars, hlit_pivots, HLIT_TIMEFRAME_KEY
    return bars, confirmed_pivots(bars, PivotConfig()), _EXECUTION_TIMEFRAME_KEY


def _exhaustion(
    inputs: AssemblyInputs,
    bars: tuple[CompletedOhlcvBar, ...],
    zones: EvidenceItem[object],
    pivots: tuple[Pivot, ...],
    timeframe: str,
) -> EvidenceItem[object]:
    if not bars or zones.state is not EvidenceState.AVAILABLE:
        return _unavailable("EXHAUSTION_INPUTS_UNAVAILABLE")
    try:
        facts = evaluate_exhaustion(
            bars,
            zones=cast(ZoneFacts, zones.value),
            pivots=pivots,
        )
    except ValueError:
        return _unavailable("EXHAUSTION_EVIDENCE_INVALID")
    return _available(
        inputs,
        key="exhaustion",
        value=facts,
        observed_at=bars[-1].timestamp,
        payload={
            "bullish": facts.bullish is not None,
            "bearish": facts.bearish is not None,
            "timeframe": timeframe,
        },
    )


def _order_flow(inputs: AssemblyInputs) -> EvidenceItem[object]:
    if inputs.market is not V6Market.BINANCE_USDM:
        return EvidenceItem(
            state=EvidenceState.NOT_APPLICABLE,
            value=None,
            provenance=None,
            blocker_code="ORDER_FLOW_BINANCE_ONLY",
        )
    if inputs.trades is None or inputs.order_flow_thresholds is None:
        return _unavailable("ORDER_FLOW_UNAVAILABLE")
    facts = aggregate_order_flow(
        inputs.trades,
        window_start=inputs.decision_at - _ORDER_FLOW_WINDOW,
        window_end=inputs.decision_at,
        thresholds=inputs.order_flow_thresholds,
    )
    return _available(
        inputs,
        key="order_flow",
        value=facts,
        observed_at=inputs.decision_at,
        payload={"trades": facts.trade_count},
    )


def _profile(inputs: AssemblyInputs) -> EvidenceItem[object]:
    if inputs.market is not V6Market.BINANCE_USDM:
        return EvidenceItem(
            state=EvidenceState.NOT_APPLICABLE,
            value=None,
            provenance=None,
            blocker_code="PROFILE_BINANCE_ONLY",
        )
    if inputs.trades is None or inputs.tick_size is None:
        return _unavailable("PROFILE_UNAVAILABLE")
    facts = build_profile(inputs.trades, tick_size=inputs.tick_size)
    return _available(
        inputs,
        key="profile",
        value=facts,
        observed_at=inputs.decision_at,
        payload={"levels": len(facts.levels)},
    )


def _calendar(inputs: AssemblyInputs) -> EvidenceItem[object]:
    if inputs.events is None:
        return _unavailable("CALENDAR_UNAVAILABLE")
    facts = evaluate_event_window(inputs.events, inputs.decision_at)
    return _available(
        inputs,
        key="calendar",
        value=facts,
        observed_at=inputs.events.captured_at,
        payload={"events": len(inputs.events.events)},
    )


def _session(inputs: AssemblyInputs) -> EvidenceItem[object]:
    calendar = inputs.calendar
    if calendar is None:
        return _unavailable("SESSION_CALENDAR_UNAVAILABLE")
    if calendar.kind is not _MARKET_SESSION_KINDS[inputs.market]:
        return _unavailable("SESSION_CALENDAR_MARKET_MISMATCH")
    facts = evaluate_session(
        calendar,
        inputs.decision_at,
        market_safety=inputs.market_safety,
    )
    return _available(
        inputs,
        key="session",
        value=facts,
        observed_at=calendar.captured_at,
        payload={"kind": calendar.kind.value},
    )


def _costs(inputs: AssemblyInputs) -> EvidenceItem[object]:
    if (
        inputs.fee_schedule is None
        or inputs.spread is None
        or inputs.quantity is None
        or inputs.stop_slippage_q95 is None
        or inputs.tick_size is None
    ):
        return _unavailable("COSTS_UNAVAILABLE")
    facts = estimate_round_trip_cost(
        spread=inputs.spread,
        quantity=inputs.quantity,
        fee_schedule=inputs.fee_schedule,
        stop_slippage_q95=inputs.stop_slippage_q95,
        tick_size=inputs.tick_size,
    )
    return _available(
        inputs,
        key="costs",
        value=facts,
        observed_at=inputs.source.captured_at,
        payload={"state": facts.state.value},
    )


def _available(
    inputs: AssemblyInputs,
    *,
    key: str,
    value: object,
    observed_at: datetime | None,
    payload: Mapping[str, object],
) -> EvidenceItem[object]:
    return EvidenceItem(
        state=EvidenceState.AVAILABLE,
        value=value,
        provenance=_provenance(
            inputs,
            key=key,
            observed_at=observed_at or inputs.source.captured_at,
            payload=payload,
        ),
        blocker_code=None,
    )


def _unavailable(blocker_code: str) -> EvidenceItem[object]:
    return EvidenceItem(
        state=EvidenceState.UNAVAILABLE,
        value=None,
        provenance=None,
        blocker_code=blocker_code,
    )


def _provenance(
    inputs: AssemblyInputs,
    *,
    key: str,
    observed_at: datetime,
    payload: Mapping[str, object],
) -> EvidenceProvenance:
    observed = observed_at.astimezone(UTC)
    captured = inputs.source.captured_at
    return EvidenceProvenance(
        source=inputs.source.name,
        source_key=f"{inputs.market.value}:{key}",
        source_timezone=inputs.source.timezone,
        observed_at=observed,
        captured_at=max(captured, observed),
        digest_sha256=_digest(
            {
                "market": inputs.market.value,
                "key": key,
                "observed_at": observed.isoformat(),
                "payload": dict(payload),
            }
        ),
    )


def _bars_payload(bars: Sequence[CompletedOhlcvBar]) -> Mapping[str, object]:
    return {
        "count": len(bars),
        "first": bars[0].timestamp.isoformat(),
        "last": bars[-1].timestamp.isoformat(),
        "close": _decimal(bars[-1].close),
    }


def _digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _decimal(value: Decimal) -> str:
    normalized = require_decimal(value)
    return "0" if normalized.is_zero() else format(normalized.normalize(), "f")


__all__ = (
    "HLIT_TIMEFRAME_KEY",
    "AssemblyInputs",
    "AssemblyResult",
    "AssemblySource",
    "assemble_v6_evidence",
    "derive_indicators",
)
