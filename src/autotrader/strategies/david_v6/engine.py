from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol, cast

from autotrader.domain.enums import IntentType, Side
from autotrader.risk.v6 import V6RiskContext, evaluate_v6_risk
from autotrader.strategies.common.decisions import StrategyDecision
from autotrader.strategies.david_v6.calendar import CalendarFacts
from autotrader.strategies.david_v6.costs import CostFacts
from autotrader.strategies.david_v6.evidence import EvidenceItem, V6EvidenceBundle
from autotrader.strategies.david_v6.exhaustion import ExhaustionFacts
from autotrader.strategies.david_v6.grading import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
    grade_setup,
)
from autotrader.strategies.david_v6.manifest import V6Manifest
from autotrader.strategies.david_v6.metodo import MetodoFacts
from autotrader.strategies.david_v6.models import (
    EvidenceState,
    MatchedIndicator,
    SetupGrade,
    StrategyFamily,
    V6Decision,
    V6Market,
)
from autotrader.strategies.david_v6.order_flow import (
    OrderFlowFacts,
    blocking_big_trade_ahead,
)
from autotrader.strategies.david_v6.pivots import DivergenceFacts, DivergenceKind
from autotrader.strategies.david_v6.profile import ProfileFacts
from autotrader.strategies.david_v6.regime import RegimeFacts
from autotrader.strategies.david_v6.sessions import SessionFacts
from autotrader.strategies.david_v6.universe import UniverseFacts
from autotrader.strategies.david_v6.zones import ZoneFacts

_REGULAR_KIND = {
    Side.BUY: DivergenceKind.REGULAR_BULLISH,
    Side.SELL: DivergenceKind.REGULAR_BEARISH,
}

_COMMON_REQUIRED = frozenset({"regime", "calendar", "session", "costs"})
_METODO_REQUIRED = frozenset({"universe", "metodo"})
_CASH_HLIT_REQUIRED = frozenset({"universe", "zones", "divergence", "exhaustion"})
_BINANCE_HLIT_REQUIRED = frozenset(
    {"zones", "divergence", "exhaustion", "order_flow", "profile"}
)
_FACT_TYPES: dict[str, type[object]] = {
    "universe": UniverseFacts,
    "regime": RegimeFacts,
    "metodo": MetodoFacts,
    "zones": ZoneFacts,
    "divergence": DivergenceFacts,
    "exhaustion": ExhaustionFacts,
    "order_flow": OrderFlowFacts,
    "profile": ProfileFacts,
    "calendar": CalendarFacts,
    "session": SessionFacts,
    "costs": CostFacts,
}


class _StateFact(Protocol):
    state: EvidenceState


def evaluate_v6(
    bundle: V6EvidenceBundle,
    *,
    manifest: V6Manifest,
    risk_context: V6RiskContext,
) -> V6Decision:
    if type(bundle) is not V6EvidenceBundle:
        raise TypeError("bundle must be an exact V6EvidenceBundle")
    if type(manifest) is not V6Manifest:
        raise TypeError("manifest must be an exact V6Manifest")
    if type(risk_context) is not V6RiskContext:
        raise TypeError("risk_context must be an exact V6RiskContext")
    manifest.__post_init__()
    risk_context.__post_init__()
    facts = _facts(bundle)
    blockers: list[str] = []
    if risk_context.risk_request.market is not bundle.market:
        blockers.append("RISK_MARKET_MISMATCH")
    if (
        risk_context.family is StrategyFamily.METODO
        and bundle.market is V6Market.BINANCE_USDM
    ):
        blockers.append("METODO_CASH_ONLY")
    if (
        bundle.market in {V6Market.KRX_CASH, V6Market.US_CASH}
        and risk_context.risk_request.side is Side.SELL
    ):
        blockers.append("CASH_SHORT_UNSUPPORTED")

    required = _required_keys(bundle.market, risk_context.family)
    for key in sorted(required):
        item = facts[key]
        if item.state is not EvidenceState.AVAILABLE:
            blockers.append(item.blocker_code or f"{key.upper()}_{item.state.value}")
    _semantic_blockers(
        facts,
        required,
        risk_context.risk_request.side,
        risk_context.risk_request.entry_price,
        blockers,
    )

    provenance_hashes, completed_at = _provenance(bundle)
    valid_indicators: list[MatchedIndicator] = []
    for indicator in risk_context.matched_indicators:
        if indicator.evidence_hash not in provenance_hashes:
            blockers.append(f"INDICATOR_PROVENANCE_MISSING:{indicator.key}")
            continue
        valid_indicators.append(indicator)
    direction_code = (
        DIRECTION_LONG
        if risk_context.risk_request.side is Side.BUY
        else DIRECTION_SHORT
    )
    if direction_code not in {indicator.key for indicator in valid_indicators}:
        blockers.append("DIRECTION_EVIDENCE_MISSING")

    indicators = tuple(sorted(valid_indicators, key=lambda indicator: indicator.key))
    grade = grade_setup(
        indicators,
        mandatory_codes=risk_context.mandatory_indicator_codes,
    )
    if grade is SetupGrade.REJECT:
        blockers.append("CONTRADICTORY_DIRECTION_EVIDENCE")
    if (
        bundle.market in {V6Market.KRX_CASH, V6Market.US_CASH}
        and grade is SetupGrade.A_CANDIDATE
    ):
        grade = SetupGrade.NORMAL

    preliminary_grade = SetupGrade.REJECT if blockers else grade
    risk_request = replace(
        risk_context.risk_request,
        grade=preliminary_grade,
    )
    authority = evaluate_v6_risk(risk_request)
    blockers.extend(authority.blocker_codes)
    canonical_blockers = tuple(sorted(set(blockers)))
    final_grade = SetupGrade.REJECT if canonical_blockers else grade
    tradeable = final_grade is not SetupGrade.REJECT
    quantity = authority.quantity if tradeable else Decimal(0)
    source_hashes = tuple(
        sorted(
            {
                manifest.source_sha256,
                manifest.design_sha256,
                manifest.configuration_hash,
                *provenance_hashes,
            }
        )
    )
    return V6Decision(
        id=risk_context.decision_id,
        strategy_version_id=manifest.strategy_version_id,
        setup_id=risk_context.setup_id,
        feature_snapshot_id=risk_context.feature_snapshot_id,
        instrument_id=bundle.instrument_id,
        market=bundle.market,
        family=risk_context.family,
        grade=final_grade,
        side=risk_context.risk_request.side,
        order_style=risk_context.order_style,
        matched_indicators=indicators,
        blockers=canonical_blockers,
        planned_entry=(risk_context.risk_request.entry_price if tradeable else None),
        structural_stop=(authority.stop_price if tradeable else None),
        target_price=(risk_context.target_price if tradeable else None),
        risk_fraction=(authority.risk_fraction if tradeable else Decimal(0)),
        calculated_quantity=quantity,
        expected_cost=(
            risk_context.risk_request.cost_per_unit * quantity if tradeable else None
        ),
        source_evidence_hashes=source_hashes,
        completed_evidence_at=completed_at,
        generated_at=bundle.decision_at,
        valid_until=risk_context.valid_until,
    )


def to_strategy_decision(decision: V6Decision) -> StrategyDecision:
    if type(decision) is not V6Decision or decision.grade is SetupGrade.REJECT:
        raise ValueError("tradeable exact V6Decision is required")
    assert decision.planned_entry is not None
    assert decision.structural_stop is not None
    return StrategyDecision(
        id=decision.id,
        strategy_version_id=decision.strategy_version_id,
        setup_id=decision.setup_id,
        feature_snapshot_id=decision.feature_snapshot_id,
        instrument_id=decision.instrument_id,
        intent_type=IntentType.ENTRY,
        side=decision.side,
        order_style=decision.order_style,
        planned_entry=decision.planned_entry,
        trigger_price=decision.planned_entry,
        invalidation_price=decision.structural_stop,
        generated_at=decision.generated_at,
        valid_until=decision.valid_until,
        session_type=decision.market.value,
        source_v6_decision_id=decision.id,
    )


def _required_keys(market: V6Market, family: StrategyFamily) -> frozenset[str]:
    if family is StrategyFamily.METODO:
        return _COMMON_REQUIRED | _METODO_REQUIRED
    if market is V6Market.BINANCE_USDM:
        return _COMMON_REQUIRED | _BINANCE_HLIT_REQUIRED
    return _COMMON_REQUIRED | _CASH_HLIT_REQUIRED


def _facts(bundle: V6EvidenceBundle) -> dict[str, EvidenceItem[object]]:
    return {
        "universe": bundle.universe,
        "regime": bundle.regime,
        "metodo": bundle.metodo,
        "zones": bundle.zones,
        "divergence": bundle.divergence,
        "exhaustion": bundle.exhaustion,
        "order_flow": bundle.order_flow,
        "profile": bundle.profile,
        "calendar": bundle.calendar,
        "session": bundle.session,
        "costs": bundle.costs,
    }


def _semantic_blockers(
    facts: dict[str, EvidenceItem[object]],
    required: frozenset[str],
    side: Side,
    entry_price: Decimal,
    blockers: list[str],
) -> None:
    valid_values: dict[str, object] = {}
    for key in sorted(required):
        item = facts[key]
        if item.state is not EvidenceState.AVAILABLE:
            continue
        value = item.value
        if type(value) is not _FACT_TYPES[key]:
            blockers.append(f"{key.upper()}_VALUE_INVALID")
            continue
        valid_values[key] = value

    for key in ("regime", "order_flow", "profile", "calendar", "session", "costs"):
        value = valid_values.get(key)
        if value is not None:
            internal_state = cast(_StateFact, value).state
            if internal_state is not EvidenceState.AVAILABLE:
                blockers.append(f"{key.upper()}_FACT_{internal_state.value}")

    checks = {
        "universe": ("eligible", "UNIVERSE_INELIGIBLE", True),
        "regime": ("excluded", "REGIME_EXCLUDED", False),
        "metodo": ("normal_technical_confirmation", "METODO_GATE_FAILED", True),
        "calendar": ("block_new_exposure", "CALENDAR_BLOCKED", False),
        "session": ("entry_allowed", "SESSION_ENTRY_BLOCKED", True),
    }
    for key, (attribute, blocker, required_value) in checks.items():
        value = valid_values.get(key)
        if value is None:
            continue
        observed = getattr(value, attribute)
        if observed is not required_value:
            blockers.append(blocker)

    _hlit_blockers(valid_values, side, entry_price, blockers)


def _hlit_blockers(
    valid_values: dict[str, object],
    side: Side,
    entry_price: Decimal,
    blockers: list[str],
) -> None:
    """Enforce the drawing preconditions of the HLIT specification.

    A regular divergence in the traded direction must exist before any level is
    drawn, a pre-marked zone must exist, and reaching that zone is never enough
    on its own: a confirmed exhaustion sequence in the same direction is
    required.
    """
    divergence = valid_values.get("divergence")
    if divergence is not None:
        matching = tuple(
            signal
            for signal in cast(DivergenceFacts, divergence).regular
            if signal.kind is _REGULAR_KIND[side]
        )
        if not matching:
            blockers.append("REGULAR_DIVERGENCE_ABSENT")

    zones = valid_values.get("zones")
    if zones is not None and not cast(ZoneFacts, zones).zones:
        blockers.append("NO_MARKED_ZONE")

    exhaustion = valid_values.get("exhaustion")
    if exhaustion is not None:
        facts = cast(ExhaustionFacts, exhaustion)
        sequence = facts.bullish if side is Side.BUY else facts.bearish
        if sequence is None:
            blockers.append("EXHAUSTION_ABSENT")
        elif sequence.direction is not side:
            blockers.append("EXHAUSTION_DIRECTION_MISMATCH")
        elif not sequence.confirmed:
            blockers.append("EXHAUSTION_UNCONFIRMED")

    order_flow = valid_values.get("order_flow")
    if order_flow is not None and blocking_big_trade_ahead(
        cast(OrderFlowFacts, order_flow),
        side=side,
        reference_price=entry_price,
    ):
        blockers.append("BLOCKING_BIG_TRADE_AHEAD")


def _provenance(bundle: V6EvidenceBundle) -> tuple[set[bytes], datetime]:
    items = (*bundle.bars.values(), *_facts(bundle).values())
    provenances = tuple(
        item.provenance
        for item in items
        if item.state is EvidenceState.AVAILABLE and item.provenance is not None
    )
    hashes = {bytes.fromhex(item.digest_sha256) for item in provenances}
    completed_at = max(
        (item.observed_at for item in provenances),
        default=bundle.decision_at,
    )
    return hashes, completed_at


__all__ = ("evaluate_v6", "to_strategy_decision")
