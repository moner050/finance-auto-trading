"""One pass of the trading loop.

The spine is short on purpose: refuse to act when trading is disarmed,
assemble the evidence, evaluate it, record whatever came out, and only then
consider sending anything. Recording happens for every evaluation, including a
rejection, because the blockers are the answer to "why did nothing happen"
and that question is asked far more often than the other one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol, cast
from uuid import UUID

from autotrader.apps.trader.position_management import PositionMarket
from autotrader.domain.enums import Side
from autotrader.risk.v6 import V6RiskContext
from autotrader.shared.time import require_utc
from autotrader.strategies.common.decisions import StrategyDecision
from autotrader.strategies.david_v6.assembly import (
    HLIT_TIMEFRAME_KEY,
    AssemblyInputs,
    AssemblyResult,
    assemble_v6_evidence,
    derive_indicators,
)
from autotrader.strategies.david_v6.engine import evaluate_v6, to_strategy_decision
from autotrader.strategies.david_v6.exhaustion import ExhaustionFacts
from autotrader.strategies.david_v6.hlit import HlitSetup
from autotrader.strategies.david_v6.manifest import V6Manifest
from autotrader.strategies.david_v6.models import (
    EvidenceState,
    SetupGrade,
    V6Decision,
)

DISARMED = "DISARMED"
NO_SETUP = "NO_SETUP_DRAWN"
POSITION_MANAGED = "POSITION_MANAGED"
NOT_TRADEABLE = "NOT_TRADEABLE"
SUBMITTED = "SUBMITTED"


class TradingControl(Protocol):
    async def is_armed(self) -> bool:
        """Whether new exposure is currently permitted."""
        ...


class DecisionRecorder(Protocol):
    async def record(self, decision: V6Decision) -> None:
        """Persist one evaluation, tradeable or not."""
        ...


class PositionManagement(Protocol):
    async def manage(self, market: PositionMarket, *, now: datetime) -> bool:
        """Act on what is held, reporting whether anything was done."""
        ...


class Execution(Protocol):
    async def submit(
        self,
        *,
        decision: V6Decision,
        strategy_decision: StrategyDecision,
        setup: HlitSetup,
        now: datetime,
    ) -> UUID | None:
        """Turn a tradeable decision into an order, returning its id."""
        ...


@dataclass(frozen=True, slots=True)
class TickOutcome:
    reason: str
    decision: V6Decision | None
    order_id: UUID | None

    @property
    def blockers(self) -> tuple[str, ...]:
        return () if self.decision is None else self.decision.blockers


@dataclass(frozen=True, slots=True)
class TickContext:
    inputs: AssemblyInputs
    manifest: V6Manifest
    risk_context: V6RiskContext
    now: datetime

    def __post_init__(self) -> None:
        if type(cast(object, self.inputs)) is not AssemblyInputs:
            raise TypeError("inputs must be exact AssemblyInputs")
        if type(cast(object, self.manifest)) is not V6Manifest:
            raise TypeError("manifest must be an exact V6Manifest")
        if type(cast(object, self.risk_context)) is not V6RiskContext:
            raise TypeError("risk_context must be an exact V6RiskContext")
        object.__setattr__(self, "now", require_utc(self.now))


def _position_market(
    context: TickContext, assembled: AssemblyResult
) -> PositionMarket | None:
    """The market as the position manager needs it, or None.

    None when a piece it cannot do without is absent - the tick's inputs make
    the tick size and the fee schedule optional, and both price what an exit
    would cost. Skipping the pass leaves the position where it was, behind the
    stop it already has, which beats managing it against a gap.
    """
    bars = context.inputs.bars.get(HLIT_TIMEFRAME_KEY)
    if not bars or context.inputs.tick_size is None:
        return None
    if context.inputs.fee_schedule is None:
        return None
    return PositionMarket(
        bundle=assembled.bundle,
        hlit=assembled.hlit,
        current_price=bars[-1].close,
        atr_5m=context.risk_context.risk_request.atr_5m,
        tick_size=context.inputs.tick_size,
        fee_schedule=context.inputs.fee_schedule,
        stop_slippage_q95=context.inputs.stop_slippage_q95,
    )


async def run_tick(
    context: TickContext,
    *,
    control: TradingControl,
    recorder: DecisionRecorder,
    execution: Execution,
    position: PositionManagement | None = None,
) -> TickOutcome:
    if type(context) is not TickContext:
        raise TypeError("context must be an exact TickContext")
    context.__post_init__()
    # Ask before doing any work: a disarmed account should cost nothing and,
    # more importantly, must leave no trace of an evaluation it never made.
    if not await control.is_armed():
        return TickOutcome(reason=DISARMED, decision=None, order_id=None)

    assembled = assemble_v6_evidence(context.inputs)
    market = _position_market(context, assembled)
    if (
        position is not None
        and market is not None
        and await position.manage(market, now=context.now)
    ):
        # Deal with what is held, and stop there when something was done.
        # Closing a position and opening another against the same bar would
        # be two decisions on one reading, and the second would be sized
        # against exposure that had just changed underneath it.
        return TickOutcome(reason=POSITION_MANAGED, decision=None, order_id=None)

    side = context.risk_context.risk_request.side
    decision = evaluate_v6(
        assembled.bundle,
        manifest=context.manifest,
        risk_context=_from_evidence(context.risk_context, assembled, side),
    )
    await recorder.record(decision)

    if decision.grade is SetupGrade.REJECT:
        return TickOutcome(reason=NOT_TRADEABLE, decision=decision, order_id=None)
    setup = None if assembled.hlit is None else assembled.hlit.for_side(side)
    if setup is None:
        # The engine gates on a regular divergence, so a tradeable decision
        # without drawn levels would mean the two disagree.
        return TickOutcome(reason=NO_SETUP, decision=decision, order_id=None)

    order_id = await execution.submit(
        decision=decision,
        strategy_decision=to_strategy_decision(decision),
        setup=setup,
        now=context.now,
    )
    if order_id is None:
        return TickOutcome(reason=NOT_TRADEABLE, decision=decision, order_id=None)
    return TickOutcome(reason=SUBMITTED, decision=decision, order_id=order_id)


def _from_evidence(
    risk_context: V6RiskContext,
    assembled: AssemblyResult,
    side: Side,
) -> V6RiskContext:
    """Replace whatever the caller claimed with what the evidence supports."""
    derived = derive_indicators(
        assembled,
        side=side,
        reference_price=risk_context.risk_request.entry_price,
    )
    return replace(
        risk_context,
        matched_indicators=derived,
        risk_request=replace(
            risk_context.risk_request,
            structural_reference=_structural_reference(risk_context, assembled, side),
        ),
    )


def _structural_reference(
    risk_context: V6RiskContext,
    assembled: AssemblyResult,
    side: Side,
) -> Decimal:
    """Section 9.2 puts the stop outside the confirmed low or high of the leg.

    That price is the exhaustion's own reference, so a caller's guess never
    stands in for it. Without a confirmed sequence the caller's value is kept
    and the engine's exhaustion gate rejects the setup anyway.
    """
    item = assembled.bundle.exhaustion
    if item.state is not EvidenceState.AVAILABLE:
        return risk_context.risk_request.structural_reference
    facts = cast(ExhaustionFacts, item.value)
    sequence = facts.bullish if side is Side.BUY else facts.bearish
    if sequence is None or not sequence.confirmed:
        return risk_context.risk_request.structural_reference
    return sequence.structural_reference_price


__all__ = (
    "DISARMED",
    "NOT_TRADEABLE",
    "NO_SETUP",
    "POSITION_MANAGED",
    "SUBMITTED",
    "DecisionRecorder",
    "Execution",
    "TickContext",
    "TickOutcome",
    "TradingControl",
    "run_tick",
)
