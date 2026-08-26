"""The dispatch store against a real MySQL.

What matters here is what survives a crash: the attempt marker is written
before the broker is reached, it can only be claimed once, and an accepted
send is never overwritten by a later report.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

import pytest
from integration.execution.test_order_command_idempotency import _seed
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.execution.orders.service import OrderService, OrderSubmissionContext
from autotrader.persistence.mysql.dispatch_store import (
    ACCEPTED,
    REJECTED,
    UNKNOWN,
    MySqlDispatchStore,
)
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.orders import (
    PersistedBrokerOrderLink,
    PersistedOrderCommand,
)
from autotrader.persistence.mysql.repositories.orders import MySqlOrderStore

NOW = datetime(2026, 8, 9, tzinfo=UTC)


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url is None:
        pytest.skip("DATABASE_URL is required for MySQL integration tests")
    return url


async def _command_id(sessions: async_sessionmaker[object]) -> UUID:
    """Create one approved order and return its submit command."""
    intent, decision = await _seed(sessions)
    submission = OrderSubmissionContext(
        broker_client_order_id=f"order-{intent.id.hex}",
        owner_runtime_instance_id=uuid7(),
        fencing_token=1,
        not_after=NOW + timedelta(minutes=1),
        time_in_force="DAY",
        authority_class="SUBMIT_NEW_EXPOSURE",
        created_at=NOW,
    )
    async with sessions() as session:  # type: ignore[operator]
        await OrderService(store=MySqlOrderStore(session)).create_from_risk_decision(
            decision=decision, intent=intent, submission=submission
        )
        await session.commit()
    async with sessions() as session:  # type: ignore[operator]
        command_id = await session.scalar(select(PersistedOrderCommand.id))
        assert command_id is not None
        return command_id


@pytest.mark.integration
def test_dispatch_store_records_what_crossed_the_broker_boundary() -> None:
    url = _database_url()

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            command_id = await _command_id(sessions)

            # Claiming marks the attempt before anything is sent.
            async with sessions() as session:
                store = MySqlDispatchStore(session)
                claimed = await store.authorize_and_record_attempt(
                    command_id=command_id, now=NOW
                )
                assert claimed is not None
                assert claimed.id == command_id
                await session.commit()
            async with sessions() as session:
                row = await session.get(PersistedOrderCommand, command_id)
                assert row is not None
                assert row.dispatch_attempted_at is not None
                assert row.result_state is None

            # A second claim finds the marker and refuses.
            async with sessions() as session:
                store = MySqlDispatchStore(session)
                assert (
                    await store.authorize_and_record_attempt(
                        command_id=command_id, now=NOW
                    )
                    is None
                )
                recovery = await store.recoverable_command(command_id=command_id)
                assert recovery is not None
                assert recovery.id == command_id

            async with sessions() as session:
                store = MySqlDispatchStore(session)
                await store.record_accepted(
                    command_id=command_id, broker_order_id="B-1", now=NOW
                )
                await session.commit()
            async with sessions() as session:
                row = await session.get(PersistedOrderCommand, command_id)
                assert row is not None
                assert row.result_state == ACCEPTED
                link = await session.scalar(
                    select(PersistedBrokerOrderLink).where(
                        PersistedBrokerOrderLink.order_id == row.order_id
                    )
                )
                assert link is not None
                assert link.broker_order_id == "B-1"
                assert link.exposure_bearing is True
        finally:
            await engine.dispose()

    asyncio.run(verify())


@pytest.mark.integration
def test_an_accepted_send_is_never_overwritten() -> None:
    url = _database_url()

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            command_id = await _command_id(sessions)
            async with sessions() as session:
                store = MySqlDispatchStore(session)
                await store.authorize_and_record_attempt(command_id=command_id, now=NOW)
                await store.record_accepted(
                    command_id=command_id, broker_order_id="B-2", now=NOW
                )
                # A late unknown or rejection must not erase the acceptance.
                await store.record_unknown(
                    command_id=command_id, now=NOW, deadline=NOW + timedelta(minutes=1)
                )
                await store.record_rejected(command_id=command_id, now=NOW)
                await session.commit()

            async with sessions() as session:
                row = await session.get(PersistedOrderCommand, command_id)
                assert row is not None
                assert row.result_state == ACCEPTED
                # Recording accepted twice must not add a second link.
                links = await session.scalar(
                    select(func.count(PersistedBrokerOrderLink.id))
                )
                assert links == 1
                store = MySqlDispatchStore(session)
                assert await store.recoverable_command(command_id=command_id) is None
        finally:
            await engine.dispose()

    asyncio.run(verify())


@pytest.mark.integration
def test_a_rejected_send_is_terminal_and_an_unknown_one_is_recoverable() -> None:
    url = _database_url()

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            command_id = await _command_id(sessions)
            async with sessions() as session:
                store = MySqlDispatchStore(session)
                await store.authorize_and_record_attempt(command_id=command_id, now=NOW)
                await store.record_unknown(
                    command_id=command_id, now=NOW, deadline=NOW + timedelta(minutes=1)
                )
                await session.commit()

            async with sessions() as session:
                row = await session.get(PersistedOrderCommand, command_id)
                assert row is not None
                assert row.result_state == UNKNOWN
                store = MySqlDispatchStore(session)
                # Unknown means the outcome is still open, so recovery may run.
                assert (
                    await store.recoverable_command(command_id=command_id) is not None
                )
                await store.record_rejected(command_id=command_id, now=NOW)
                await session.commit()

            async with sessions() as session:
                row = await session.get(PersistedOrderCommand, command_id)
                assert row is not None
                assert row.result_state == REJECTED
                store = MySqlDispatchStore(session)
                assert await store.recoverable_command(command_id=command_id) is None
        finally:
            await engine.dispose()

    asyncio.run(verify())


@pytest.mark.integration
def test_an_expired_command_is_never_claimed() -> None:
    url = _database_url()

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            command_id = await _command_id(sessions)
            async with sessions() as session:
                store = MySqlDispatchStore(session)
                claimed = await store.authorize_and_record_attempt(
                    command_id=command_id, now=NOW + timedelta(hours=1)
                )
                assert claimed is None
                await session.commit()
            async with sessions() as session:
                row = await session.get(PersistedOrderCommand, command_id)
                assert row is not None
                assert row.dispatch_attempted_at is None
        finally:
            await engine.dispose()

    asyncio.run(verify())
