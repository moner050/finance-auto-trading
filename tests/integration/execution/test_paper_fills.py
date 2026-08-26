"""Paper orders stage when sent and settle when their bar closes.

The bar that fills an order has not closed when the order is sent, so a paper
fill cannot be synchronous without look-ahead. These check the gap is real and
that nothing invents a fill to close it early.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from conftest import integration_database_url
from integration.execution.test_dispatch_store import _command_id
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.integrations.brokers.internal_paper import (
    InternalPaperBroker,
    PaperExecutionBar,
    PaperOrderCommand,
    PaperOrderStatus,
)
from autotrader.integrations.brokers.paper_submitter import (
    PaperAccount,
    PaperBrokerSubmitter,
    resolve_paper_fills,
)
from autotrader.persistence.mysql.dispatch_store import ACCEPTED, MySqlDispatchStore
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.orders import PersistedOrderCommand
from autotrader.persistence.mysql.models.paper import PaperOrderRow
from autotrader.persistence.mysql.paper_journal import MySqlPaperJournal
from autotrader.strategies.david_v6.models import V6Market

FIVE_MINUTES = timedelta(minutes=5)
NOW = datetime(2026, 8, 9, tzinfo=UTC)


def _database_url() -> str:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")
    return url


def _account() -> PaperAccount:
    return PaperAccount(
        account_alias="internal-us-paper",
        market=V6Market.US_CASH,
        timeframe=FIVE_MINUTES,
        fee_per_unit=Decimal("0.01"),
        slippage_per_unit=Decimal("0.01"),
    )


class _NoBarYet:
    """Market data for the moment before the fill bar has closed."""

    async def bar_at(self, command: PaperOrderCommand) -> PaperExecutionBar | None:
        del command
        return None

    async def next_bar(self, command: PaperOrderCommand) -> PaperExecutionBar | None:
        del command
        return None


class _BarHasClosed:
    """A bar that straddles the order's limit, so either side can fill."""

    def _bar(self, command: PaperOrderCommand) -> PaperExecutionBar:
        centre = command.limit_price or Decimal("100")
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

    async def bar_at(self, command: PaperOrderCommand) -> PaperExecutionBar | None:
        return self._bar(command)

    async def next_bar(self, command: PaperOrderCommand) -> PaperExecutionBar | None:
        return self._bar(command)


@pytest.mark.integration
def test_a_sent_paper_order_is_staged_and_not_yet_filled() -> None:
    url = _database_url()

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            command_id = await _command_id(sessions)
            async with sessions() as session:
                store = MySqlDispatchStore(session)
                command = await store.authorize_and_record_attempt(
                    command_id=command_id, now=NOW
                )
                assert command is not None
                submitter = PaperBrokerSubmitter(
                    journal=MySqlPaperJournal(session), account=_account()
                )
                submission = await submitter.submit(command)
                await store.record_accepted(
                    command_id=command_id,
                    broker_order_id=submission.broker_order_id,
                    now=NOW,
                )
                await session.commit()

            async with sessions() as session:
                row = await session.get(PaperOrderRow, command_id)
                assert row is not None
                # Staged, with nothing claimed about the fill.
                assert row.status is None
                assert row.filled_quantity is None
                assert row.fill_price is None
                dispatched = await session.get(PersistedOrderCommand, command_id)
                assert dispatched is not None
                assert dispatched.result_state == ACCEPTED
        finally:
            await engine.dispose()

    asyncio.run(verify())


@pytest.mark.integration
def test_a_staged_order_waits_while_its_bar_has_not_closed() -> None:
    url = _database_url()

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            command_id = await _stage(sessions)
            async with sessions() as session:
                journal = MySqlPaperJournal(session)
                resolved = await resolve_paper_fills(
                    broker=InternalPaperBroker(
                        journal=journal, market_data=_NoBarYet()
                    ),
                    journal=journal,
                    bars=_NoBarYet(),
                )
                await session.commit()

            assert resolved == ()
            async with sessions() as session:
                row = await session.get(PaperOrderRow, command_id)
                assert row is not None
                # Still staged: a bar that has not closed is not a missing bar.
                assert row.status is None
        finally:
            await engine.dispose()

    asyncio.run(verify())


@pytest.mark.integration
def test_a_closed_bar_settles_the_order_once() -> None:
    url = _database_url()

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            command_id = await _stage(sessions)
            bars = _BarHasClosed()
            async with sessions() as session:
                journal = MySqlPaperJournal(session)
                resolved = await resolve_paper_fills(
                    broker=InternalPaperBroker(journal=journal, market_data=bars),
                    journal=journal,
                    bars=bars,
                )
                await session.commit()

            assert len(resolved) == 1
            assert resolved[0].status is PaperOrderStatus.FILLED, resolved[
                0
            ].reason_code
            async with sessions() as session:
                row = await session.get(PaperOrderRow, command_id)
                assert row is not None
                assert row.status == PaperOrderStatus.FILLED.value
                assert row.filled_quantity == row.quantity
                assert row.fill_price is not None
                assert row.filled_at is not None

            # A second pass has nothing left to settle.
            async with sessions() as session:
                journal = MySqlPaperJournal(session)
                again = await resolve_paper_fills(
                    broker=InternalPaperBroker(journal=journal, market_data=bars),
                    journal=journal,
                    bars=bars,
                )
                await session.commit()
            assert again == ()
        finally:
            await engine.dispose()

    asyncio.run(verify())


async def _stage(sessions: async_sessionmaker[object]) -> object:
    command_id = await _command_id(sessions)
    async with sessions() as session:  # type: ignore[operator]
        store = MySqlDispatchStore(session)
        command = await store.authorize_and_record_attempt(
            command_id=command_id, now=NOW
        )
        assert command is not None
        await PaperBrokerSubmitter(
            journal=MySqlPaperJournal(session), account=_account()
        ).submit(command)
        await session.commit()
    return command_id


@pytest.mark.integration
def test_only_submit_commands_reach_the_paper_broker() -> None:
    url = _database_url()

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            command_id = await _command_id(sessions)
            async with sessions() as session:
                command = await session.scalar(
                    select(PersistedOrderCommand).where(
                        PersistedOrderCommand.id == command_id
                    )
                )
                assert command is not None
                submitter = PaperBrokerSubmitter(
                    journal=MySqlPaperJournal(session), account=_account()
                )
                store = MySqlDispatchStore(session)
                broker_command = await store.authorize_and_record_attempt(
                    command_id=command_id, now=NOW
                )
                assert broker_command is not None
                with pytest.raises(ValueError, match="cannot cancel"):
                    await submitter.cancel(broker_command)
                with pytest.raises(ValueError, match="cannot replace"):
                    await submitter.replace(broker_command)
        finally:
            await engine.dispose()

    asyncio.run(verify())
