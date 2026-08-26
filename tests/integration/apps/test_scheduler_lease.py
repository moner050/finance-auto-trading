"""The lease against a real MySQL.

Two loops trading one account is the worst thing this system could do, so the
lease is the guarantee that matters most in the driver.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest
from conftest import integration_database_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.apps.trader.composition import LeaseSettings, MySqlSchedulerLease
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine

NOW = datetime(2026, 8, 9, tzinfo=UTC)
TTL = timedelta(minutes=5)


def _database_url() -> str:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")
    return url


def _lease(sessions: async_sessionmaker[object], name: str) -> MySqlSchedulerLease:
    return MySqlSchedulerLease(
        sessions,  # type: ignore[arg-type]
        LeaseSettings(lease_name=name, runtime_instance_id=uuid7(), ttl=TTL),
    )


@pytest.mark.integration
def test_only_one_instance_holds_the_lease_at_a_time() -> None:
    url = _database_url()

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            name = f"trader:{uuid7().hex[:16]}"
            first = _lease(sessions, name)
            second = _lease(sessions, name)

            assert await first.acquire(NOW) is True
            # The second instance is a different runtime and must stand down.
            assert await second.acquire(NOW) is False
            # The holder keeps it across passes.
            assert await first.acquire(NOW + timedelta(seconds=30)) is True
        finally:
            await engine.dispose()

    asyncio.run(verify())


@pytest.mark.integration
def test_a_lease_that_has_expired_can_be_taken_over() -> None:
    url = _database_url()

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            name = f"trader:{uuid7().hex[:16]}"
            first = _lease(sessions, name)
            second = _lease(sessions, name)

            assert await first.acquire(NOW) is True
            assert await second.acquire(NOW + timedelta(seconds=30)) is False
            # Once the holder's lease has lapsed, another instance may lead.
            assert await second.acquire(NOW + TTL + timedelta(seconds=1)) is True
        finally:
            await engine.dispose()

    asyncio.run(verify())


@pytest.mark.integration
def test_separate_lease_names_do_not_block_each_other() -> None:
    url = _database_url()

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            krx = _lease(sessions, f"krx:{uuid7().hex[:12]}")
            us = _lease(sessions, f"us:{uuid7().hex[:12]}")

            # One loop per market, each with its own lease.
            assert await krx.acquire(NOW) is True
            assert await us.acquire(NOW) is True
        finally:
            await engine.dispose()

    asyncio.run(verify())
