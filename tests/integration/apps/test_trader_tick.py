"""One tick end to end against a real MySQL.

This is the question the whole rebuild turns on: does a completed bar reach a
recorded decision, and does a tradeable one reach an order that crossed a
broker boundary.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

import pytest
from conftest import integration_database_url
from integration.risk.test_concurrent_reservation import _seed as _risk_seed
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from unit.apps.trader.test_tick import _quiet_calendar, _risk_context
from unit.strategies.david_v6.test_assembly import (
    SESSION_OPEN,
    _daily_bars,
    _decelerating_decline_bars,
    _inputs,
)
from unit.strategies.david_v6.test_assembly import V6Market as Market

from autotrader.apps.trader.composition import (
    ExecutionAccount,
    MySqlDecisionRecorder,
    MySqlPaperExecution,
    MySqlTradingControl,
)
from autotrader.apps.trader.tick import DISARMED, SUBMITTED, TickContext, run_tick
from autotrader.config.settings import RuntimeMode, Settings
from autotrader.execution.intents.models import AccountCandidate
from autotrader.integrations.brokers.fake.adapter import FakeBroker, FakeBrokerScenario
from autotrader.persistence.mysql.dispatch_store import ACCEPTED, RuntimeFacts
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.intents import PersistedOrderIntent
from autotrader.persistence.mysql.models.operations import OpsTradingControl
from autotrader.persistence.mysql.models.orders import (
    PersistedOrder,
    PersistedOrderCommand,
)
from autotrader.persistence.mysql.models.strategy import (
    StrategyDefinition,
    StrategySignal,
    StrategyVersion,
)
from autotrader.persistence.mysql.repositories.david_v6 import DavidV6Repository
from autotrader.strategies.david_v6.manifest import (
    STRATEGY_CODE,
    STRATEGY_VERSION,
    V6_DESIGN_SHA256,
    V6_SOURCE_SHA256,
    V6Manifest,
    v6_configuration_hash,
)
from autotrader.strategies.david_v6.models import SetupGrade

NOW = datetime(2026, 8, 9, tzinfo=UTC)


def _database_url() -> str:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")
    return url


async def _arm(sessions: async_sessionmaker[object], *, armed: bool) -> None:
    async with sessions() as session:  # type: ignore[operator]
        session.add(
            OpsTradingControl(
                scope_type="GLOBAL",
                scope_key="ALL",
                armed=armed,
                kill_switch_level="NONE",
                owner_runtime_instance_id=None,
                acquired_at=None,
                expires_at=None,
                fencing_token=1,
                row_version=1,
            )
        )
        await session.commit()


async def _register_strategy(
    sessions: async_sessionmaker[object], version_id: UUID
) -> V6Manifest:
    """The manifest needs a registered version, so create one for this run."""
    async with sessions() as session:  # type: ignore[operator]
        definition_id = uuid7()
        session.add(
            StrategyDefinition(
                id=definition_id,
                code=STRATEGY_CODE,
                research_only=False,
                configuration_hash=v6_configuration_hash(),
            )
        )
        await session.flush()
        session.add(
            StrategyVersion(
                id=version_id,
                definition_id=definition_id,
                version=STRATEGY_VERSION,
                status="SHADOW",
                research_only=False,
            )
        )
        await session.commit()
    manifest = V6Manifest(
        id=uuid7(),
        strategy_version_id=version_id,
        source_sha256=V6_SOURCE_SHA256,
        design_sha256=V6_DESIGN_SHA256,
        configuration_hash=v6_configuration_hash(),
        registered_at=NOW - timedelta(days=1),
    )
    async with sessions() as session:  # type: ignore[operator]
        await DavidV6Repository(session).persist_manifest(manifest)
        await session.commit()
    return manifest


def _context(manifest: V6Manifest, instrument_id: UUID) -> TickContext:
    risk_context = _risk_context()
    return TickContext(
        inputs=_inputs(
            Market.US_CASH,
            instrument_id=instrument_id,
            bars={"5m": _decelerating_decline_bars(), "1d": _daily_bars()},
            events=_quiet_calendar(),
        ),
        manifest=manifest,
        risk_context=risk_context,
        now=SESSION_OPEN,
    )


@pytest.mark.integration
def test_a_disarmed_control_leaves_no_trace_of_a_tick() -> None:
    url = _database_url()

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            ids = await _risk_seed(sessions)
            await _arm(sessions, armed=False)
            manifest = await _register_strategy(sessions, uuid7())

            outcome = await run_tick(
                _context(manifest, ids.instrument_id),
                control=MySqlTradingControl(sessions),
                recorder=MySqlDecisionRecorder(sessions),
                execution=MySqlPaperExecution(
                    sessions=sessions,
                    account=_account(ids),
                    broker=FakeBroker(scenario=FakeBrokerScenario.FULL_FILL),
                ),
            )

            assert outcome.reason == DISARMED
            async with sessions() as session:
                assert await session.scalar(select(func.count(PersistedOrder.id))) == 0
        finally:
            await engine.dispose()

    asyncio.run(verify())


def _account(ids: object) -> ExecutionAccount:
    return ExecutionAccount(
        account=AccountCandidate(
            id=ids.account_id,  # type: ignore[attr-defined]
            broker_code="TEST",
            market_code="US",
            environment="PAPER",
            enabled=True,
            policy_key="risk",
            policy_active=True,
        ),
        policy_version_id=ids.policy_version_id,  # type: ignore[attr-defined]
        risk_snapshot_id=ids.risk_snapshot_id,  # type: ignore[attr-defined]
        currency="USD",
        facts=RuntimeFacts(
            runtime_mode=RuntimeMode.PAPER,
            allow_live=False,
            account_environment=RuntimeMode.PAPER,
            local_runtime_instance_id=ids.runtime_instance_id,  # type: ignore[attr-defined]
            market_data_fresh=lambda: True,
        ),
        runtime_instance_id=uuid7(),
        fencing_token=1,
    )


@pytest.mark.integration
def test_a_tradeable_tick_records_a_decision_and_reaches_the_broker() -> None:
    url = _database_url()

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            ids = await _risk_seed(sessions)
            await _arm(sessions, armed=True)
            manifest = await _register_strategy(sessions, uuid7())

            outcome = await run_tick(
                _context(manifest, ids.instrument_id),
                control=MySqlTradingControl(sessions),
                recorder=MySqlDecisionRecorder(sessions),
                execution=MySqlPaperExecution(
                    sessions=sessions,
                    account=_account(ids),
                    broker=FakeBroker(scenario=FakeBrokerScenario.FULL_FILL),
                ),
            )

            assert outcome.decision is not None
            assert outcome.decision.grade is not SetupGrade.REJECT, outcome.blockers
            assert outcome.reason == SUBMITTED

            async with sessions() as session:
                # The decision the backoffice will show.
                blockers = await DavidV6Repository(session).blocker_codes(
                    outcome.decision.id
                )
                assert blockers == ()
                # The signal that bridges a v6 decision to execution.
                assert (
                    await session.get(StrategySignal, outcome.decision.id) is not None
                )
                assert (
                    await session.scalar(select(func.count(PersistedOrderIntent.id)))
                    >= 1
                )
                assert await session.scalar(select(func.count(PersistedOrder.id))) == 1
                command = await session.scalar(select(PersistedOrderCommand))
                assert command is not None
                # The marker written before the broker was reached, then the
                # outcome the broker gave back.
                assert command.dispatch_attempted_at is not None
                assert command.result_state == ACCEPTED
        finally:
            await engine.dispose()

    asyncio.run(verify())
