"""The operations projection against a real MySQL.

What matters here is not that the queries run. It is that the screen cannot
tell the operator something the loop disagrees with, and that nothing the
projection returns carries a secret.
"""

from __future__ import annotations

import asyncio
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from uuid import UUID, uuid7

import pytest
from conftest import integration_database_url
from integration.risk.test_concurrent_reservation import _seed as _risk_seed
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.read_model import (
    ControlView,
    DecisionView,
    DriftView,
    IncidentView,
    OperationsReadModel,
    PositionView,
)
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.operations import (
    OpsIncident,
    OpsTradingControl,
)
from autotrader.persistence.mysql.models.positions import Position

NOW = datetime(2026, 8, 27, tzinfo=UTC)

_FORBIDDEN = (
    "ciphertext",
    "nonce",
    "secret",
    "token",
    "api_key",
    "access_token",
    "refresh_token",
    "verifier",
    "account_number",
)


def _drive(scenario: object) -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")

    async def run() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await scenario(sessions)  # type: ignore[operator]
        finally:
            await engine.dispose()

    asyncio.run(run())


async def _control(
    sessions: async_sessionmaker[object], *, armed: bool, kill_switch: str
) -> None:
    async with sessions() as session:  # type: ignore[operator]
        session.add(
            OpsTradingControl(
                scope_type="GLOBAL",
                scope_key="ALL",
                armed=armed,
                kill_switch_level=kill_switch,
                owner_runtime_instance_id=None,
                acquired_at=None,
                expires_at=None,
                fencing_token=1,
                row_version=1,
            )
        )
        await session.commit()


async def _hold(
    sessions: async_sessionmaker[object], *, account_id: UUID, instrument_id: UUID
) -> None:
    async with sessions() as session:  # type: ignore[operator]
        session.add(
            Position(
                id=uuid7(),
                account_id=account_id,
                instrument_id=instrument_id,
                quantity=Decimal("3"),
                average_cost=Decimal("100"),
                currency="USD",
                settlement_asset=None,
                observed_at=NOW,
                blocking_risk=False,
            )
        )
        await session.commit()


@pytest.mark.integration
def test_the_screen_reports_armed_only_when_the_loop_would() -> None:
    async def scenario(sessions: async_sessionmaker[object]) -> None:
        await _control(sessions, armed=True, kill_switch="NONE")

        async with sessions() as session:  # type: ignore[operator]
            view = await OperationsReadModel(session).load(account_id=uuid7())
        assert view.armed is True

    _drive(scenario)


@pytest.mark.integration
def test_a_kill_switch_is_not_armed_however_the_flag_reads() -> None:
    async def scenario(sessions: async_sessionmaker[object]) -> None:
        # The row still says armed. The loop refuses anyway, and so must the
        # screen, or an operator reads "running" while nothing is running.
        await _control(sessions, armed=True, kill_switch="BLOCK_NEW_EXPOSURE")

        async with sessions() as session:  # type: ignore[operator]
            view = await OperationsReadModel(session).load(account_id=uuid7())

        assert view.armed is False
        assert view.controls[0].kill_switch_level == "BLOCK_NEW_EXPOSURE"

    _drive(scenario)


@pytest.mark.integration
def test_no_control_row_at_all_is_not_armed() -> None:
    async def scenario(sessions: async_sessionmaker[object]) -> None:
        async with sessions() as session:  # type: ignore[operator]
            view = await OperationsReadModel(session).load(account_id=uuid7())

        # Nobody armed anything, which is not the same as armed.
        assert view.controls == ()
        assert view.armed is False

    _drive(scenario)


@pytest.mark.integration
def test_a_position_with_no_stop_behind_it_shows_as_unprotected() -> None:
    async def scenario(sessions: async_sessionmaker[object]) -> None:
        ids = await _risk_seed(sessions)
        await _hold(
            sessions, account_id=ids.account_id, instrument_id=ids.instrument_id
        )

        async with sessions() as session:  # type: ignore[operator]
            view = await OperationsReadModel(session).load(account_id=ids.account_id)

        assert len(view.positions) == 1
        position = view.positions[0]
        assert position.quantity == Decimal("3")
        assert position.protected is False
        assert view.unprotected_positions == (position,)

    _drive(scenario)


@pytest.mark.integration
def test_open_incidents_reach_the_screen() -> None:
    async def scenario(sessions: async_sessionmaker[object]) -> None:
        async with sessions() as session:  # type: ignore[operator]
            session.add(
                OpsIncident(
                    severity="BLOCKING",
                    status="OPEN",
                    reason_code="POSITION_WITHOUT_PROTECTION",
                    scope_type="INSTRUMENT",
                    scope_key="an-instrument",
                    created_at=NOW,
                )
            )
            session.add(
                OpsIncident(
                    severity="INFO",
                    status="RESOLVED",
                    reason_code="ALREADY_HANDLED",
                    scope_type=None,
                    scope_key=None,
                    created_at=NOW,
                )
            )
            await session.commit()

        async with sessions() as session:  # type: ignore[operator]
            view = await OperationsReadModel(session).load(account_id=uuid7())

        # A resolved incident is history, not something to act on.
        assert [incident.reason_code for incident in view.incidents] == [
            "POSITION_WITHOUT_PROTECTION"
        ]

    _drive(scenario)


@pytest.mark.integration
def test_a_limit_outside_its_range_is_refused() -> None:
    async def scenario(sessions: async_sessionmaker[object]) -> None:
        async with sessions() as session:  # type: ignore[operator]
            model = OperationsReadModel(session)
            for limit in (0, 201):
                with pytest.raises(ValueError, match="between 1 and 200"):
                    await model.decisions(limit=limit)

    _drive(scenario)


def test_no_projection_field_can_carry_a_secret() -> None:
    """A field named for a credential is how one reaches a template."""
    for view in (ControlView, DecisionView, PositionView, DriftView, IncidentView):
        names = {field.name for field in fields(view)}
        assert not any(
            forbidden in name for name in names for forbidden in _FORBIDDEN
        ), f"{view.__name__} exposes {names}"


@pytest.mark.integration
def test_the_day_is_whole_even_where_nothing_was_evaluated() -> None:
    """The gaps are the point.

    A GROUP BY returns the hours that have rows, so an idle night comes back
    as an absence and the chart would draw a busy day with the quiet part cut
    out. The buckets are generated from the clock and filled from the query,
    and this is what says so.
    """

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        async with sessions() as session:
            buckets = await OperationsReadModel(session).activity(now=NOW)

        assert len(buckets) == 24
        assert buckets[-1].hour == NOW.replace(minute=0, second=0, microsecond=0)
        assert buckets[0].hour == buckets[-1].hour - timedelta(hours=23)
        # Contiguous and in order: the axis labels are printed by position.
        for earlier, later in pairwise(buckets):
            assert later.hour - earlier.hour == timedelta(hours=1)

    _drive(scenario)


@pytest.mark.integration
def test_the_window_is_refused_rather_than_silently_clamped() -> None:
    """A caller asking for a year would ask the database for a year."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        async with sessions() as session:
            model = OperationsReadModel(session)
            for hours in (0, -1, 169):
                with pytest.raises(ValueError):
                    await model.activity(hours=hours, now=NOW)

    _drive(scenario)
