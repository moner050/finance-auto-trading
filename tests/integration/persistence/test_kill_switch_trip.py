"""Halting the account from inside the strategy, against a real MySQL.

The emergency exit closes a position and then halts. Before this there was one
way to write the kill switch column - `create_control`, which also rewrites
ownership, arming and expiry. That is right when an operator takes control and
wrong when the strategy stops itself: a halt that quietly renewed a lease would
hand the account back to whoever was holding it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest
from conftest import integration_database_url
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.execution.controls.models import KillSwitchLevel
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.operations import (
    OpsAuditLog,
    OpsTradingControl,
)
from autotrader.persistence.mysql.repositories.operations import trip_kill_switch

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

pytestmark = pytest.mark.integration


def _drive(scenario: object) -> None:
    url = integration_database_url()
    assert url is not None

    async def run() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await scenario(sessions)  # type: ignore[operator]
        finally:
            await engine.dispose()

    asyncio.run(run())


async def _seed(sessions: object, level: KillSwitchLevel, key: str) -> None:
    async with sessions() as session:  # type: ignore[operator]
        await session.execute(delete(OpsAuditLog))
        await session.execute(delete(OpsTradingControl))
        session.add(
            OpsTradingControl(
                scope_type="ACCOUNT",
                scope_key=key,
                armed=True,
                kill_switch_level=level.value,
                owner_runtime_instance_id=uuid7(),
                acquired_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
                fencing_token=7,
                row_version=3,
            )
        )
        await session.commit()


def test_a_halt_raises_the_level_and_leaves_the_lease_alone() -> None:
    async def scenario(sessions: object) -> None:
        await _seed(sessions, KillSwitchLevel.NONE, "raise")
        async with sessions() as session:  # type: ignore[operator]
            before = await session.scalar(select(OpsTradingControl))
            assert before is not None
            owner, acquired, expires = (
                before.owner_runtime_instance_id,
                before.acquired_at,
                before.expires_at,
            )
            raised = await trip_kill_switch(
                session, level=KillSwitchLevel.EMERGENCY, now=NOW
            )
            await session.commit()
        assert raised == 1
        async with sessions() as session:  # type: ignore[operator]
            after = await session.scalar(select(OpsTradingControl))
            assert after is not None
            assert after.kill_switch_level == KillSwitchLevel.EMERGENCY.value
            # The lease is untouched: a halt must not hand the account back.
            assert after.owner_runtime_instance_id == owner
            assert after.acquired_at == acquired
            assert after.expires_at == expires
            assert after.armed is True

    _drive(scenario)


def test_a_milder_halt_never_lowers_a_standing_one() -> None:
    """Two halts arriving out of order must not leave the weaker one in
    force. Coming back down is an operator's act, through the back office,
    where the second password is."""

    async def scenario(sessions: object) -> None:
        await _seed(sessions, KillSwitchLevel.EMERGENCY, "lower")
        async with sessions() as session:  # type: ignore[operator]
            raised = await trip_kill_switch(
                session, level=KillSwitchLevel.BLOCK_NEW_EXPOSURE, now=NOW
            )
            await session.commit()
        assert raised == 0
        async with sessions() as session:  # type: ignore[operator]
            after = await session.scalar(select(OpsTradingControl))
            assert after is not None
            assert after.kill_switch_level == KillSwitchLevel.EMERGENCY.value

    _drive(scenario)


def test_the_raise_is_recorded_with_what_it_changed() -> None:
    async def scenario(sessions: object) -> None:
        await _seed(sessions, KillSwitchLevel.NONE, "audit")
        async with sessions() as session:  # type: ignore[operator]
            await trip_kill_switch(session, level=KillSwitchLevel.EMERGENCY, now=NOW)
            await session.commit()
        async with sessions() as session:  # type: ignore[operator]
            entry = await session.scalar(select(OpsAuditLog))
            assert entry is not None
            assert entry.action == "STRATEGY_KILL_SWITCH_RAISED"
            assert entry.details["from"] == KillSwitchLevel.NONE.value
            assert entry.details["to"] == KillSwitchLevel.EMERGENCY.value

    _drive(scenario)
