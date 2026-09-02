from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from unit.strategies.david_v6.test_assembly import (
    SESSION_OPEN,
    _daily_bars,
    _decelerating_decline_bars,
    _inputs,
)
from unit.strategies.david_v6.test_assembly import V6Market as Market

from autotrader.apps.trader.tick import (
    DISARMED,
    NOT_TRADEABLE,
    SUBMITTED,
    TickContext,
    run_tick,
)
from autotrader.domain.enums import OrderStyle, Side
from autotrader.persistence.mysql.repositories.david_v6_risk import (
    approved_v6_policy,
)
from autotrader.risk.models import V6RiskPolicySnapshot
from autotrader.risk.v6 import V6RiskContext, V6RiskRequest
from autotrader.shared.ids import new_uuid7
from autotrader.strategies.common.decisions import StrategyDecision
from autotrader.strategies.david_v6.assembly import (
    AssemblyInputs,
    assemble_v6_evidence,
)
from autotrader.strategies.david_v6.calendar import EventCalendar
from autotrader.strategies.david_v6.hlit import HlitSetup
from autotrader.strategies.david_v6.manifest import (
    V6_DESIGN_SHA256,
    V6_SOURCE_SHA256,
    V6Manifest,
    v6_configuration_hash,
)
from autotrader.strategies.david_v6.models import SetupGrade, StrategyFamily, V6Decision


def _policy(market: Market = Market.US_CASH) -> V6RiskPolicySnapshot:
    """The approved policy, which is what the engine sizes against."""
    return approved_v6_policy(market, policy_version_id=new_uuid7())


class _Control:
    def __init__(self, armed: bool) -> None:
        self.armed = armed
        self.asked = 0

    async def is_armed(self) -> bool:
        self.asked += 1
        return self.armed


class _Recorder:
    def __init__(self) -> None:
        self.recorded: list[V6Decision] = []

    async def record(self, decision: V6Decision) -> None:
        self.recorded.append(decision)


class _Execution:
    def __init__(self, order_id: UUID | None = None) -> None:
        self.order_id = order_id
        self.calls: list[tuple[V6Decision, StrategyDecision, HlitSetup]] = []

    async def submit(
        self,
        *,
        decision: V6Decision,
        strategy_decision: StrategyDecision,
        setup: HlitSetup,
        now: datetime,
    ) -> UUID | None:
        del now
        self.calls.append((decision, strategy_decision, setup))
        return self.order_id


def _manifest() -> V6Manifest:
    return V6Manifest(
        id=new_uuid7(),
        strategy_version_id=new_uuid7(),
        source_sha256=V6_SOURCE_SHA256,
        design_sha256=V6_DESIGN_SHA256,
        configuration_hash=v6_configuration_hash(),
        registered_at=SESSION_OPEN - timedelta(days=1),
    )


def _risk_context(side: Side = Side.BUY) -> V6RiskContext:
    return V6RiskContext(
        decision_id=new_uuid7(),
        setup_id=new_uuid7(),
        feature_snapshot_id=new_uuid7(),
        family=StrategyFamily.HLIT,
        order_style=OrderStyle.LIMIT,
        # The tick replaces these with what the evidence actually supports.
        matched_indicators=(),
        mandatory_indicator_codes=frozenset(),
        # The policy is for the market the request names, which the
        # context refuses to let drift apart.
        policy=_policy(Market.US_CASH),
        risk_request=V6RiskRequest(
            market=Market.US_CASH,
            grade=SetupGrade.NORMAL,
            side=side,
            entry_price=Decimal("119.8"),
            structural_reference=Decimal("119.7"),
            tick_size=Decimal("0.01"),
            spread=Decimal("0.01"),
            atr_30s=None,
            atr_5m=Decimal("0.2"),
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
        target_price=Decimal("120.4"),
        valid_until=SESSION_OPEN + timedelta(minutes=5),
    )


def _quiet_calendar() -> EventCalendar:
    """A calendar that was fetched and found nothing scheduled."""
    return EventCalendar(
        captured_at=SESSION_OPEN - timedelta(hours=12),
        valid_until=SESSION_OPEN + timedelta(hours=12),
        events=(),
    )


def _setup_context() -> TickContext:
    return TickContext(
        inputs=_inputs(
            Market.US_CASH,
            bars={"5m": _decelerating_decline_bars(), "1d": _daily_bars()},
            events=_quiet_calendar(),
        ),
        manifest=_manifest(),
        risk_context=_risk_context(),
        now=SESSION_OPEN,
    )


def _bare_context() -> TickContext:
    return TickContext(
        inputs=_inputs(Market.US_CASH),
        manifest=_manifest(),
        risk_context=_risk_context(),
        now=SESSION_OPEN,
    )


@pytest.mark.asyncio
async def test_a_disarmed_account_evaluates_nothing_at_all() -> None:
    control, recorder, execution = _Control(False), _Recorder(), _Execution()

    outcome = await run_tick(
        _setup_context(), control=control, recorder=recorder, execution=execution
    )

    assert outcome.reason == DISARMED
    assert outcome.decision is None
    assert outcome.order_id is None
    assert recorder.recorded == []
    assert execution.calls == []


@pytest.mark.asyncio
async def test_a_rejected_evaluation_is_still_recorded_with_its_blockers() -> None:
    control, recorder, execution = _Control(True), _Recorder(), _Execution()

    outcome = await run_tick(
        _bare_context(), control=control, recorder=recorder, execution=execution
    )

    assert outcome.reason == NOT_TRADEABLE
    assert outcome.decision is not None
    assert outcome.decision.grade is SetupGrade.REJECT
    assert outcome.blockers != ()
    # The reason nothing happened is the point of the record.
    assert len(recorder.recorded) == 1
    assert recorder.recorded[0].blockers == outcome.blockers
    assert execution.calls == []


@pytest.mark.asyncio
async def test_a_rejected_evaluation_never_reaches_execution() -> None:
    control, recorder, execution = _Control(True), _Recorder(), _Execution(new_uuid7())

    outcome = await run_tick(
        _bare_context(), control=control, recorder=recorder, execution=execution
    )

    assert outcome.order_id is None
    assert execution.calls == []


@pytest.mark.asyncio
async def test_a_tradeable_setup_reaches_execution_with_its_drawn_levels() -> None:
    order_id = new_uuid7()
    control, recorder, execution = _Control(True), _Recorder(), _Execution(order_id)

    outcome = await run_tick(
        _setup_context(), control=control, recorder=recorder, execution=execution
    )

    assert outcome.decision is not None
    assert outcome.decision.grade is not SetupGrade.REJECT, outcome.blockers
    assert outcome.reason == SUBMITTED
    assert outcome.order_id == order_id
    assert len(recorder.recorded) == 1
    submitted_decision, strategy_decision, setup = execution.calls[0]
    assert submitted_decision is outcome.decision
    assert strategy_decision.side is Side.BUY
    # Section 3: the target handed to execution is the 66 percent level.
    assert setup.target_price == setup.fib_66
    assert setup.anchor_b < setup.fib_66 < setup.anchor_a


@pytest.mark.asyncio
async def test_execution_declining_leaves_the_decision_recorded() -> None:
    control, recorder, execution = _Control(True), _Recorder(), _Execution(None)

    outcome = await run_tick(
        _setup_context(), control=control, recorder=recorder, execution=execution
    )

    assert outcome.order_id is None
    assert len(recorder.recorded) == 1


@pytest.mark.asyncio
async def test_the_control_is_consulted_once_per_tick() -> None:
    control, recorder, execution = _Control(True), _Recorder(), _Execution()

    await run_tick(
        _bare_context(), control=control, recorder=recorder, execution=execution
    )

    assert control.asked == 1


@pytest.mark.asyncio
async def test_the_context_must_be_exact() -> None:
    with pytest.raises(TypeError, match="exact TickContext"):
        await run_tick(
            object(),  # type: ignore[arg-type]
            control=_Control(True),
            recorder=_Recorder(),
            execution=_Execution(),
        )


def test_inputs_must_be_exact_assembly_inputs() -> None:
    with pytest.raises(TypeError, match="exact AssemblyInputs"):
        TickContext(
            inputs=cast_object(),
            manifest=_manifest(),
            risk_context=_risk_context(),
            now=SESSION_OPEN,
        )


def cast_object() -> AssemblyInputs:
    return object()  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_the_stop_sits_at_the_exhaustion_low_not_the_caller_guess() -> None:
    """Section 9.2 places the stop outside the confirmed low of the leg."""
    context = _setup_context()
    # A caller asking for a stop far below the leg must not get one.
    wrong = replace(
        context,
        risk_context=replace(
            context.risk_context,
            risk_request=replace(
                context.risk_context.risk_request,
                structural_reference=Decimal("100.0"),
            ),
        ),
    )

    outcome = await run_tick(
        wrong,
        control=_Control(True),
        recorder=_Recorder(),
        execution=_Execution(new_uuid7()),
    )

    assert outcome.decision is not None
    assert outcome.decision.grade is not SetupGrade.REJECT, outcome.blockers
    exhaustion = assemble_v6_evidence(wrong.inputs).bundle.exhaustion.value
    assert exhaustion is not None
    sequence = exhaustion.bullish  # type: ignore[attr-defined]
    assert sequence is not None
    reference = sequence.structural_reference_price
    stop = outcome.decision.structural_stop
    assert stop is not None
    # The stop sits just under the exhaustion low, nowhere near the guess.
    assert stop < reference
    assert reference - stop < Decimal("1")
    assert stop > Decimal("119")


@pytest.mark.asyncio
async def test_without_a_confirmed_exhaustion_the_caller_value_stands() -> None:
    context = _bare_context()
    guessed = context.risk_context.risk_request.structural_reference

    outcome = await run_tick(
        context,
        control=_Control(True),
        recorder=_Recorder(),
        execution=_Execution(),
    )

    # Nothing to take a reference from, and the exhaustion gate rejects anyway.
    assert outcome.decision is not None
    assert "EXHAUSTION_ABSENT" in outcome.blockers
    assert context.risk_context.risk_request.structural_reference == guessed
