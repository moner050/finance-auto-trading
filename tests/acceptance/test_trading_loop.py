"""The trading loop as an operator runs it, against a real MySQL.

The integration suite checks one tick, or one adapter, at a time. What an
operator actually depends on is the pass: hold the lease, settle what the last
pass left open, evaluate the bar. These are the contracts that have to hold
when all of that runs together, because a hole between two correct pieces is
still a hole.

The market data is scripted so the outcome is a property of the code rather
than of whatever Binance happened to print. Everything below the loop is the
real thing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid7

import pytest
from conftest import integration_database_url
from integration.apps.test_trader_tick import (
    NOW,
    _account,
    _arm,
    _context,
    _register_strategy,
)
from integration.risk.test_concurrent_reservation import _seed as _risk_seed
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.apps.trader.binance_paper import SessionPaperJournal
from autotrader.apps.trader.composition import (
    LeaseSettings,
    MySqlDecisionRecorder,
    MySqlFillSettlement,
    MySqlPaperExecution,
    MySqlProtectionGuard,
    MySqlSchedulerLease,
    MySqlTradingControl,
)
from autotrader.apps.trader.loop import (
    NO_NEW_BAR,
    NOT_LEADER,
    UNPROTECTED,
    LoopPorts,
    run_pass,
)
from autotrader.apps.trader.market_data import HLIT_TIMEFRAME
from autotrader.apps.trader.tick import DISARMED, SUBMITTED, TickContext
from autotrader.config.settings import Settings
from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import IntentType
from autotrader.integrations.brokers.internal_paper import (
    PaperExecutionBar,
    PaperOrderCommand,
)
from autotrader.integrations.brokers.paper_submitter import (
    PaperAccount,
    PaperBrokerSubmitter,
)
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.david_v6 import DavidV6DecisionRow
from autotrader.persistence.mysql.models.fills import PersistedFill
from autotrader.persistence.mysql.models.intents import PersistedOrderIntent
from autotrader.persistence.mysql.models.operations import (
    OpsIncident,
    OpsTradingControl,
)
from autotrader.persistence.mysql.models.orders import (
    PersistedOrder,
    PersistedOrderCommand,
)
from autotrader.persistence.mysql.models.paper import PaperOrderRow
from autotrader.persistence.mysql.models.positions import Position
from autotrader.strategies.david_v6.models import V6Market

TTL = timedelta(minutes=5)


def _database_url() -> str:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for acceptance tests")
    return url


class _OneClosedBar:
    """Offers a single evaluation, then reports that no new bar has closed."""

    def __init__(self, context: TickContext) -> None:
        self._pending: TickContext | None = context

    async def context_for(self, now: datetime) -> TickContext | None:
        del now
        pending, self._pending = self._pending, None
        return pending


class _Bars:
    """The fill bar, once the scenario says it has closed.

    A resting stop is a separate question from a closed bar: it is resolved by
    whichever bar reaches its trigger, and until the scenario says one did, it
    keeps waiting the way it would against real prices.
    """

    def __init__(self) -> None:
        self.closed = False
        self.stop_reached = False

    def _bar(self, command: PaperOrderCommand) -> PaperExecutionBar | None:
        if not self.closed:
            return None
        if command.trigger_price is not None and not self.stop_reached:
            return None
        centre = command.trigger_price or command.limit_price or Decimal("100")
        return PaperExecutionBar(
            bar=CompletedOhlcvBar(
                timestamp=command.signal_at + command.timeframe,
                open=centre,
                high=centre + Decimal("1"),
                low=centre - Decimal("1"),
                close=centre,
                volume=Decimal("1000"),
            ),
            available_quantity=command.quantity,
            source_digest=b"b" * 32,
        )

    async def bar_at(
        self, command: PaperOrderCommand, *, now: datetime
    ) -> PaperExecutionBar | None:
        del now
        return self._bar(command)

    async def next_bar(
        self, command: PaperOrderCommand, *, now: datetime
    ) -> PaperExecutionBar | None:
        del now
        return self._bar(command)


def _ports(
    sessions: async_sessionmaker[object],
    *,
    context: TickContext,
    ids: object,
    bars: _Bars,
    lease_name: str,
    runtime_instance_id: UUID | None = None,
) -> LoopPorts:
    """The loop wired to the real adapters, the way the driver wires them."""
    submitter = PaperBrokerSubmitter(
        journal=SessionPaperJournal(sessions),  # type: ignore[arg-type]
        account=PaperAccount(
            account_alias="internal-us-paper",
            market=V6Market.US_CASH,
            timeframe=HLIT_TIMEFRAME,
            fee_per_unit=Decimal("0.01"),
            slippage_per_unit=Decimal("0.01"),
        ),
    )
    return LoopPorts(
        lease=MySqlSchedulerLease(
            sessions,  # type: ignore[arg-type]
            LeaseSettings(
                lease_name=lease_name,
                runtime_instance_id=runtime_instance_id or uuid7(),
                ttl=TTL,
            ),
        ),
        settlement=MySqlFillSettlement(
            sessions=sessions,  # type: ignore[arg-type]
            bars=bars,
            account=_account(ids),
            broker=submitter,
        ),
        protection=MySqlProtectionGuard(
            sessions=sessions,  # type: ignore[arg-type]
            account=_account(ids),
        ),
        source=_OneClosedBar(context),
        control=MySqlTradingControl(sessions),  # type: ignore[arg-type]
        recorder=MySqlDecisionRecorder(sessions),  # type: ignore[arg-type]
        execution=MySqlPaperExecution(
            sessions=sessions,  # type: ignore[arg-type]
            account=_account(ids),
            broker=submitter,
        ),
    )


async def _counts(sessions: async_sessionmaker[object]) -> tuple[int, int, int]:
    """Decisions, orders and staged paper orders, which is the whole trace."""
    async with sessions() as session:  # type: ignore[operator]
        return (
            await session.scalar(select(func.count(DavidV6DecisionRow.id))) or 0,
            await session.scalar(select(func.count(PersistedOrder.id))) or 0,
            await session.scalar(select(func.count(PaperOrderRow.command_id))) or 0,
        )


def _drive(scenario: object) -> None:
    url = _database_url()

    async def run() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await scenario(sessions)  # type: ignore[operator]
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_disarmed_loop_completes_a_pass_and_writes_nothing() -> None:
    """Disarmed is not a slower loop. It is a loop that leaves no trace."""

    async def scenario(sessions: async_sessionmaker[object]) -> None:
        ids = await _risk_seed(sessions)
        await _arm(sessions, armed=False)
        manifest = await _register_strategy(sessions, uuid7())

        result = await run_pass(
            now=NOW,
            ports=_ports(
                sessions,
                context=_context(manifest, ids.instrument_id),  # type: ignore[attr-defined]
                ids=ids,
                bars=_Bars(),
                lease_name=f"acceptance:{uuid7().hex[:12]}",
            ),
        )

        assert result.reason == DISARMED
        # A tradeable bar was on offer, and still nothing was written.
        assert await _counts(sessions) == (0, 0, 0)

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_one_closed_bar_produces_exactly_one_decision_and_one_order() -> None:
    """A bar the loop has already evaluated must never be traded twice."""

    async def scenario(sessions: async_sessionmaker[object]) -> None:
        ids = await _risk_seed(sessions)
        await _arm(sessions, armed=True)
        manifest = await _register_strategy(sessions, uuid7())
        ports = _ports(
            sessions,
            context=_context(manifest, ids.instrument_id),  # type: ignore[attr-defined]
            ids=ids,
            bars=_Bars(),
            lease_name=f"acceptance:{uuid7().hex[:12]}",
        )

        first = await run_pass(now=NOW, ports=ports)
        second = await run_pass(now=NOW + timedelta(minutes=5), ports=ports)

        assert first.reason == SUBMITTED
        # The same evidence must not produce a second decision.
        assert second.reason == NO_NEW_BAR
        assert await _counts(sessions) == (1, 1, 1)

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_second_instance_stands_down_while_another_holds_the_lease() -> None:
    """Two loops trading one account is the worst thing this system could do."""

    async def scenario(sessions: async_sessionmaker[object]) -> None:
        ids = await _risk_seed(sessions)
        await _arm(sessions, armed=True)
        manifest = await _register_strategy(sessions, uuid7())
        lease_name = f"acceptance:{uuid7().hex[:12]}"
        holder = _ports(
            sessions,
            context=_context(manifest, ids.instrument_id),  # type: ignore[attr-defined]
            ids=ids,
            bars=_Bars(),
            lease_name=lease_name,
        )
        challenger = _ports(
            sessions,
            context=_context(manifest, ids.instrument_id),  # type: ignore[attr-defined]
            ids=ids,
            bars=_Bars(),
            lease_name=lease_name,
        )

        assert (await run_pass(now=NOW, ports=holder)).reason == SUBMITTED
        after_holder = await _counts(sessions)

        # The challenger has a tradeable bar of its own and must not act on it.
        result = await run_pass(now=NOW + timedelta(seconds=30), ports=challenger)

        assert result.reason == NOT_LEADER
        assert result.outcome is None
        assert await _counts(sessions) == after_holder

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_paper_order_settles_on_the_next_closed_bar_exactly_once() -> None:
    """The fill bar has not closed when the order is sent, so settling it is
    the next pass's work, and that work has to be idempotent."""

    async def scenario(sessions: async_sessionmaker[object]) -> None:
        ids = await _risk_seed(sessions)
        await _arm(sessions, armed=True)
        manifest = await _register_strategy(sessions, uuid7())
        bars = _Bars()
        ports = _ports(
            sessions,
            context=_context(manifest, ids.instrument_id),  # type: ignore[attr-defined]
            ids=ids,
            bars=bars,
            lease_name=f"acceptance:{uuid7().hex[:12]}",
        )

        submitted = await run_pass(now=NOW, ports=ports)
        assert submitted.reason == SUBMITTED
        # Nothing could have settled: the fill bar had not closed yet.
        assert submitted.settled == 0

        bars.closed = True
        assert (await run_pass(now=NOW + HLIT_TIMEFRAME, ports=ports)).settled == 1
        # A resolved order is never settled a second time.
        assert (await run_pass(now=NOW + 2 * HLIT_TIMEFRAME, ports=ports)).settled == 0

        async with sessions() as session:  # type: ignore[operator]
            row = await session.scalar(select(PaperOrderRow))
            assert row is not None
            # The receipt half of the row, written only once the bar closed.
            assert row.status is not None
            assert row.filled_at is not None
            # And the whole point of settling: the ledger now says what the
            # account holds. A fill that stops at the receipt leaves the
            # system believing it holds nothing.
            position = await session.scalar(select(Position))
            assert position is not None
            assert position.quantity == row.filled_quantity
            assert position.instrument_id == ids.instrument_id  # type: ignore[attr-defined]
            # One execution, however many times settlement runs.
            assert await session.scalar(select(func.count(PersistedFill.id))) == 1

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_filled_entry_leaves_a_stop_behind_it() -> None:
    """A position with nothing behind it is the one state this must not sit
    in. Section 9.2 named the stop when the decision was made; the fill is
    what turns it into an order."""

    async def scenario(sessions: async_sessionmaker[object]) -> None:
        ids = await _risk_seed(sessions)
        await _arm(sessions, armed=True)
        manifest = await _register_strategy(sessions, uuid7())
        bars = _Bars()
        ports = _ports(
            sessions,
            context=_context(manifest, ids.instrument_id),  # type: ignore[attr-defined]
            ids=ids,
            bars=bars,
            lease_name=f"acceptance:{uuid7().hex[:12]}",
        )

        entry = await run_pass(now=NOW, ports=ports)
        assert entry.reason == SUBMITTED
        bars.closed = True
        assert (await run_pass(now=NOW + HLIT_TIMEFRAME, ports=ports)).settled == 1

        async with sessions() as session:  # type: ignore[operator]
            decision = await session.scalar(select(DavidV6DecisionRow))
            assert decision is not None
            protection = await session.scalar(
                select(PersistedOrderIntent).where(
                    PersistedOrderIntent.intent_type == IntentType.PROTECTIVE.value
                )
            )
            assert protection is not None
            assert protection.protection_reason_code == "STRUCTURAL_STOP"
            # It closes the position rather than adding to it.
            assert protection.side != decision.side
            order = await session.scalar(
                select(PersistedOrder).where(
                    PersistedOrder.order_intent_id == protection.id
                )
            )
            assert order is not None
            # The price the strategy named, not one invented at fill time.
            assert order.trigger_price == decision.structural_stop
            command = await session.scalar(
                select(PersistedOrderCommand).where(
                    PersistedOrderCommand.order_id == order.id
                )
            )
            assert command is not None
            assert command.authority_class == "SUBMIT_STRICT_REDUCTION"
            # And it reached the broker, so it is resting rather than pending.
            assert command.result_state == "ACCEPTED"

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_stop_that_is_reached_closes_the_position() -> None:
    """The stop only means something if it fires. A bar reaching it has to
    take the position back to flat, and must not then be protected in turn."""

    async def scenario(sessions: async_sessionmaker[object]) -> None:
        ids = await _risk_seed(sessions)
        await _arm(sessions, armed=True)
        manifest = await _register_strategy(sessions, uuid7())
        bars = _Bars()
        ports = _ports(
            sessions,
            context=_context(manifest, ids.instrument_id),  # type: ignore[attr-defined]
            ids=ids,
            bars=bars,
            lease_name=f"acceptance:{uuid7().hex[:12]}",
        )

        assert (await run_pass(now=NOW, ports=ports)).reason == SUBMITTED
        bars.closed = True
        assert (await run_pass(now=NOW + HLIT_TIMEFRAME, ports=ports)).settled == 1
        async with sessions() as session:  # type: ignore[operator]
            opened = await session.scalar(select(Position))
            assert opened is not None and opened.quantity > 0

        # Now a bar reaches the stop.
        bars.stop_reached = True
        assert (await run_pass(now=NOW + 2 * HLIT_TIMEFRAME, ports=ports)).settled == 1

        async with sessions() as session:  # type: ignore[operator]
            closed = await session.scalar(select(Position))
            assert closed is not None
            assert closed.quantity == 0
            # Two executions, the entry and the stop, and no third.
            assert await session.scalar(select(func.count(PersistedFill.id))) == 2
            # A stop closing a position is not itself something to protect.
            protective = await session.scalars(
                select(PersistedOrderIntent).where(
                    PersistedOrderIntent.intent_type == IntentType.PROTECTIVE.value
                )
            )
            assert len(protective.all()) == 1

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_position_whose_stop_never_reached_the_broker_stops_the_loop() -> None:
    """Dispatch turns an indeterminate broker into UNKNOWN, which is right,
    but a stop that may or may not be resting is not protection. The loop has
    to stop opening exposure and say so."""

    async def scenario(sessions: async_sessionmaker[object]) -> None:
        ids = await _risk_seed(sessions)
        await _arm(sessions, armed=True)
        manifest = await _register_strategy(sessions, uuid7())
        bars = _Bars()
        ports = _ports(
            sessions,
            context=_context(manifest, ids.instrument_id),  # type: ignore[attr-defined]
            ids=ids,
            bars=bars,
            lease_name=f"acceptance:{uuid7().hex[:12]}",
        )

        assert (await run_pass(now=NOW, ports=ports)).reason == SUBMITTED
        bars.closed = True
        assert (await run_pass(now=NOW + HLIT_TIMEFRAME, ports=ports)).settled == 1

        # The stop was placed, but its send never came back confirmed.
        async with sessions() as session:  # type: ignore[operator]
            protective = await session.scalar(
                select(PersistedOrderCommand)
                .join(
                    PersistedOrder,
                    PersistedOrder.id == PersistedOrderCommand.order_id,
                )
                .join(
                    PersistedOrderIntent,
                    PersistedOrderIntent.id == PersistedOrder.order_intent_id,
                )
                .where(PersistedOrderIntent.intent_type == IntentType.PROTECTIVE.value)
            )
            assert protective is not None
            protective.result_state = "UNKNOWN"
            await session.commit()

        result = await run_pass(now=NOW + 2 * HLIT_TIMEFRAME, ports=ports)

        assert result.reason == UNPROTECTED
        assert result.outcome is None
        async with sessions() as session:  # type: ignore[operator]
            incident = await session.scalar(
                select(OpsIncident).where(
                    OpsIncident.reason_code == "POSITION_WITHOUT_PROTECTION"
                )
            )
            assert incident is not None
            assert incident.severity == "BLOCKING"
            assert incident.status == "OPEN"
            # New exposure is blocked, and only new exposure: a full halt
            # would also stop the stop from ever being placed again.
            control = await session.scalar(select(OpsTradingControl))
            assert control is not None
            assert control.kill_switch_level == "BLOCK_NEW_EXPOSURE"
            assert control.armed is True

    _drive(scenario)
