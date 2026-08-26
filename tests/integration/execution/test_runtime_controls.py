from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid7

import pytest
from alembic import command
from alembic.config import Config
from conftest import integration_database_url
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.execution.controls.models import KillSwitchLevel
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.operations import (
    OpsAuditLog,
    OpsSchedulerLease,
    OpsTradingControl,
)
from autotrader.persistence.mysql.repositories.operations import (
    RuntimeControlRepository,
)

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 8, 9, tzinfo=UTC)


@pytest.mark.integration
def test_expired_control_takeover_fences_disarms_and_audits() -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")
    command.upgrade(Config(ROOT / "alembic.ini"), "head")

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        old_owner = uuid7()
        new_owner = uuid7()
        try:
            async with session_factory() as session:
                repository = RuntimeControlRepository(session)
                await repository.create_control(
                    scope_type="GLOBAL",
                    scope_key="GLOBAL",
                    owner_runtime_instance_id=old_owner,
                    armed=True,
                    kill_switch_level=KillSwitchLevel.NONE,
                    acquired_at=NOW - timedelta(minutes=2),
                    expires_at=NOW - timedelta(minutes=1),
                )
                session.add(
                    OpsSchedulerLease(
                        lease_name="execution-control",
                        owner_runtime_instance_id=old_owner,
                        acquired_at=NOW - timedelta(minutes=2),
                        expires_at=NOW - timedelta(minutes=1),
                        fencing_token=1,
                        row_version=1,
                    )
                )
                await session.commit()

            async with session_factory() as session:
                acquired = await RuntimeControlRepository(
                    session
                ).start_execution_control(
                    runtime_instance_id=new_owner,
                    now=NOW,
                    lease_expires_at=NOW + timedelta(minutes=1),
                )
                await session.commit()
                assert acquired is True

            async with session_factory() as session:
                control = await session.scalar(
                    select(OpsTradingControl).where(
                        OpsTradingControl.scope_type == "GLOBAL",
                        OpsTradingControl.scope_key == "GLOBAL",
                    )
                )
                assert control is not None
                assert control.armed is False
                assert control.fencing_token == 2
                assert await session.scalar(select(func.count(OpsAuditLog.id))) == 2
        finally:
            await engine.dispose()

    asyncio.run(verify())


@pytest.mark.integration
def test_healthy_owner_stays_standby_without_disarming_or_refencing() -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")
    command.upgrade(Config(ROOT / "alembic.ini"), "head")

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        owner, standby = uuid7(), uuid7()
        try:
            async with sessions() as session:
                await RuntimeControlRepository(session).create_control(
                    scope_type="GLOBAL",
                    scope_key="GLOBAL",
                    owner_runtime_instance_id=owner,
                    armed=True,
                    kill_switch_level=KillSwitchLevel.NONE,
                    acquired_at=NOW,
                    expires_at=NOW + timedelta(minutes=1),
                )
                session.add(
                    OpsSchedulerLease(
                        lease_name="execution-control",
                        owner_runtime_instance_id=owner,
                        acquired_at=NOW,
                        expires_at=NOW + timedelta(minutes=1),
                        fencing_token=1,
                        row_version=1,
                    )
                )
                await session.commit()
            async with sessions() as session:
                assert not await RuntimeControlRepository(
                    session
                ).start_execution_control(
                    runtime_instance_id=standby,
                    now=NOW,
                    lease_expires_at=NOW + timedelta(minutes=2),
                )
                await session.commit()
            async with sessions() as session:
                control = await session.scalar(
                    select(OpsTradingControl).where(
                        OpsTradingControl.scope_type == "GLOBAL",
                        OpsTradingControl.scope_key == "GLOBAL",
                    )
                )
                lease = await session.scalar(select(OpsSchedulerLease))
                assert control is not None and lease is not None
                assert control.owner_runtime_instance_id == owner and control.armed
                assert control.fencing_token == 1 and control.row_version == 1
                assert (
                    lease.owner_runtime_instance_id == owner
                    and lease.fencing_token == 1
                )
                assert await session.scalar(select(func.count(OpsAuditLog.id))) == 1
        finally:
            await engine.dispose()

    asyncio.run(verify())
