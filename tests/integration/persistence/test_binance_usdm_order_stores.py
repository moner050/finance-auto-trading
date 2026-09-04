"""The two durable stores the live path writes to before it sends.

These run against MySQL rather than a fake, because what is being checked is
mostly the database's own behaviour: whether `INSERT IGNORE` decides a claim
atomically, whether the CHECK constraints hold a row the code would have let
through, and whether a Decimal survives a round trip through JSON and back.
A fake store would answer yes to all three by construction.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid7

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from conftest import integration_database_url
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.domain.enums import Side
from autotrader.integrations.brokers.binance_usdm.algo_order_store import (
    MySqlBinanceUsdmAlgoOrderStore,
)
from autotrader.integrations.brokers.binance_usdm.algo_orders import (
    BinanceUsdmAlgoOrderRecord,
    BinanceUsdmAlgoOrderState,
    EntryFill,
    ProtectionResult,
    binance_protection_client_algo_id,
)
from autotrader.integrations.brokers.binance_usdm.order_store import (
    BinanceUsdmOrderRecordMissing,
    MySqlBinanceUsdmNormalOrderStore,
)
from autotrader.integrations.brokers.binance_usdm.orders import (
    BinanceUsdmFill,
    BinanceUsdmNormalOrderRecord,
    BinanceUsdmNormalOrderState,
    BrokerWriteResult,
)
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.accounts import Account, Broker
from autotrader.persistence.mysql.models.bindings import ProviderAccountBinding
from autotrader.persistence.mysql.repositories.binance_usdm import (
    BinanceUsdmAlgoOrderRepository,
    BinanceUsdmNormalOrderRepository,
)

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
BODY = b"symbol=BTCUSDT&side=BUY&type=MARKET&quantity=0.002"


def _sessions() -> tuple[object, async_sessionmaker[AsyncSession]]:
    engine = create_engine(Settings())
    return engine, async_sessionmaker(bind=engine, expire_on_commit=False)


async def _binding(sessions: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    async with sessions() as session:
        existing = await session.scalar(select(Broker).where(Broker.code == "BINANCE"))
        if existing is None:
            broker = Broker(id=uuid7(), code="BINANCE", name="Binance")
            session.add(broker)
            await session.flush()
            broker_id = broker.id
        else:
            broker_id = existing.id
        account = Account(
            id=uuid7(),
            broker_id=broker_id,
            account_alias=f"order-store-{uuid7().hex[:8]}",
            environment="LIVE",
            secret_reference="secret://none",
            enabled=False,
        )
        session.add(account)
        await session.flush()
        binding = ProviderAccountBinding(
            id=uuid7(),
            account_id=account.id,
            broker_id=broker_id,
            provider_code="BINANCE",
            environment="LIVE",
            account_seq=None,
            revision=1,
            observed_at=NOW - timedelta(days=1),
            active=True,
        )
        session.add(binding)
        await session.commit()
        return binding.id, account.id


def _normal(
    *, binding_id: UUID, account_id: UUID, body: bytes = BODY
) -> BinanceUsdmNormalOrderRecord:
    command_id = uuid7()
    record = BinanceUsdmNormalOrderRecord(
        command_id=command_id,
        account_id=account_id,
        binding_id=binding_id,
        client_order_id=f"v6-{command_id.hex}",
        request_body=body,
        request_digest=sha256(body).digest(),
        prepared_at=NOW,
        not_after=NOW + timedelta(minutes=1),
        dispatch_count=1,
        state=BinanceUsdmNormalOrderState.PREPARED,
        result=None,
    )
    record.validate()
    return record


def _write_result(client_order_id: str) -> BrokerWriteResult:
    """Deliberately awkward decimals: a float round trip would lose these."""
    return BrokerWriteResult(
        broker_order_id="BINANCE-USDM:8389765812345678901",
        client_order_id=client_order_id or f"v6-{uuid7().hex}",
        provider_state="FILLED",
        cumulative_filled_quantity=Decimal("0.002"),
        cumulative_quote_quantity=Decimal("221.4130000000000001"),
        average_fill_price=Decimal("110706.50000000000005"),
        commissions=(("USDT", Decimal("0.08856520000000000004")),),
        fills=(
            BinanceUsdmFill(
                trade_id=4321,
                order_id=8389765812345678901,
                side=Side.BUY,
                quantity=Decimal("0.002"),
                price=Decimal("110706.50000000000005"),
                commission=Decimal("0.08856520000000000004"),
                commission_asset="USDT",
                realized_pnl=Decimal("-0.0000000000000001"),
                occurred_at=NOW,
            ),
        ),
        recovered=False,
    )


@pytest.mark.integration
def test_normal_order_store_claims_once_and_round_trips() -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")
    alembic_command.upgrade(Config(ROOT / "alembic.ini"), "head")

    async def verify() -> None:
        engine, sessions = _sessions()
        try:
            binding_id, account_id = await _binding(sessions)
            record = _normal(binding_id=binding_id, account_id=account_id)

            async with sessions() as session:
                store = MySqlBinanceUsdmNormalOrderStore(
                    BinanceUsdmNormalOrderRepository(session)
                )
                first = await store.prepare(record)
                await session.commit()
            assert first.acquired is True

            # A second preparation of the same command finds the row and does
            # not own it. Two owners is a duplicate order on a real account.
            async with sessions() as session:
                store = MySqlBinanceUsdmNormalOrderStore(
                    BinanceUsdmNormalOrderRepository(session)
                )
                second = await store.prepare(record)
                await session.commit()
            assert second.acquired is False
            assert second.record.dispatch_count == 1

            result = _write_result(record.client_order_id)
            async with sessions() as session:
                store = MySqlBinanceUsdmNormalOrderStore(
                    BinanceUsdmNormalOrderRepository(session)
                )
                finished = await store.finish(
                    record.client_order_id,
                    state=BinanceUsdmNormalOrderState.ACKNOWLEDGED,
                    result=result,
                )
                await session.commit()
            assert finished.state is BinanceUsdmNormalOrderState.ACKNOWLEDGED

            # Read back through a fresh session: what is checked is the
            # database's copy, not an object still in memory.
            async with sessions() as session:
                store = MySqlBinanceUsdmNormalOrderStore(
                    BinanceUsdmNormalOrderRepository(session)
                )
                reloaded = await store.load_by_client_id(record.client_order_id)
            assert reloaded is not None
            assert reloaded.result == result
        finally:
            await engine.dispose()  # pyright: ignore[reportAttributeAccessIssue]

    asyncio.run(verify())


@pytest.mark.integration
def test_normal_order_store_reopens_only_a_not_sent_record() -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")
    alembic_command.upgrade(Config(ROOT / "alembic.ini"), "head")

    async def verify() -> None:
        engine, sessions = _sessions()
        try:
            binding_id, account_id = await _binding(sessions)
            record = _normal(binding_id=binding_id, account_id=account_id)

            async with sessions() as session:
                store = MySqlBinanceUsdmNormalOrderStore(
                    BinanceUsdmNormalOrderRepository(session)
                )
                await store.prepare(record)
                await store.mark_not_sent(
                    record.client_order_id, request_digest=record.request_digest
                )
                await session.commit()

            # NOT_SENT means the request provably never reached a socket, so a
            # fresh attempt is the correct answer and the only state where it
            # is. The attempt count carries over so a record that keeps
            # failing to send is visible.
            async with sessions() as session:
                store = MySqlBinanceUsdmNormalOrderStore(
                    BinanceUsdmNormalOrderRepository(session)
                )
                retry = await store.prepare(record)
                await session.commit()
            assert retry.acquired is True
            assert retry.record.dispatch_count == 2

            # AMBIGUOUS means Binance may have it. Never a second send.
            async with sessions() as session:
                store = MySqlBinanceUsdmNormalOrderStore(
                    BinanceUsdmNormalOrderRepository(session)
                )
                await store.finish(
                    record.client_order_id,
                    state=BinanceUsdmNormalOrderState.AMBIGUOUS,
                    result=None,
                )
                await session.commit()
            async with sessions() as session:
                store = MySqlBinanceUsdmNormalOrderStore(
                    BinanceUsdmNormalOrderRepository(session)
                )
                blocked = await store.prepare(record)
            assert blocked.acquired is False
            assert blocked.record.state is BinanceUsdmNormalOrderState.AMBIGUOUS
        finally:
            await engine.dispose()  # pyright: ignore[reportAttributeAccessIssue]

    asyncio.run(verify())


@pytest.mark.integration
def test_normal_order_store_refuses_a_different_request_under_one_id() -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")
    alembic_command.upgrade(Config(ROOT / "alembic.ini"), "head")

    async def verify() -> None:
        engine, sessions = _sessions()
        try:
            binding_id, account_id = await _binding(sessions)
            record = _normal(binding_id=binding_id, account_id=account_id)
            async with sessions() as session:
                store = MySqlBinanceUsdmNormalOrderStore(
                    BinanceUsdmNormalOrderRepository(session)
                )
                await store.prepare(record)
                await session.commit()

            other = b"symbol=BTCUSDT&side=SELL&type=MARKET&quantity=0.500"
            impostor = BinanceUsdmNormalOrderRecord(
                command_id=record.command_id,
                account_id=account_id,
                binding_id=binding_id,
                client_order_id=record.client_order_id,
                request_body=other,
                request_digest=sha256(other).digest(),
                prepared_at=NOW,
                not_after=NOW + timedelta(minutes=1),
                dispatch_count=1,
                state=BinanceUsdmNormalOrderState.PREPARED,
                result=None,
            )
            async with sessions() as session:
                store = MySqlBinanceUsdmNormalOrderStore(
                    BinanceUsdmNormalOrderRepository(session)
                )
                with pytest.raises(ValueError):
                    await store.prepare(impostor)

            # Marking a digest that is not the record's would licence a send
            # of an order this caller never built.
            async with sessions() as session:
                store = MySqlBinanceUsdmNormalOrderStore(
                    BinanceUsdmNormalOrderRepository(session)
                )
                with pytest.raises(ValueError):
                    await store.mark_not_sent(
                        record.client_order_id, request_digest=sha256(other).digest()
                    )

            async with sessions() as session:
                store = MySqlBinanceUsdmNormalOrderStore(
                    BinanceUsdmNormalOrderRepository(session)
                )
                with pytest.raises(BinanceUsdmOrderRecordMissing):
                    await store.finish(
                        f"v6-{uuid7().hex}",
                        state=BinanceUsdmNormalOrderState.REJECTED,
                        result=None,
                    )
        finally:
            await engine.dispose()  # pyright: ignore[reportAttributeAccessIssue]

    asyncio.run(verify())


def _fill(binding_id: UUID, account_id: UUID) -> EntryFill:
    return EntryFill(
        entry_command_id=uuid7(),
        account_id=account_id,
        instrument_id=uuid7(),
        binding_id=binding_id,
        side=Side.BUY,
        first_fill_quantity=Decimal("0.002"),
        cumulative_quantity_before=Decimal(0),
        average_fill_price=Decimal("110706.50000000000005"),
        symbol="BTCUSDT",
        tick_size=Decimal("0.10"),
        filled_at=NOW,
        protection_deadline=NOW + timedelta(seconds=30),
        emergency_close_command_id=uuid7(),
    )


@pytest.mark.integration
def test_algo_order_store_never_lets_a_second_stop_be_placed() -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")
    alembic_command.upgrade(Config(ROOT / "alembic.ini"), "head")

    async def verify() -> None:
        engine, sessions = _sessions()
        try:
            binding_id, account_id = await _binding(sessions)
            fill = _fill(binding_id, account_id)
            body = b"algoType=CONDITIONAL&symbol=BTCUSDT&type=STOP_MARKET"
            record = BinanceUsdmAlgoOrderRecord(
                entry_fill=fill,
                client_algo_id=binance_protection_client_algo_id(fill.entry_command_id),
                trigger_price=Decimal("109500.30000000000004"),
                request_body=body,
                request_digest=sha256(body).digest(),
                prepared_at=NOW,
                state=BinanceUsdmAlgoOrderState.PREPARED,
                result=None,
            )
            record.validate()

            async with sessions() as session:
                store = MySqlBinanceUsdmAlgoOrderStore(
                    BinanceUsdmAlgoOrderRepository(session)
                )
                first = await store.prepare(record)
                await session.commit()
            assert first.acquired is True

            async with sessions() as session:
                store = MySqlBinanceUsdmAlgoOrderStore(
                    BinanceUsdmAlgoOrderRepository(session)
                )
                second = await store.prepare(record)
                await session.commit()
            assert second.acquired is False

            protection = ProtectionResult(
                provider_algo_id="BINANCE-USDM-ALGO:1234567890123456789",
                client_algo_id=record.client_algo_id,
                state=BinanceUsdmAlgoOrderState.ACTIVE,
                trigger_price=record.trigger_price,
                recovered=False,
                emergency_close=None,
            )
            async with sessions() as session:
                store = MySqlBinanceUsdmAlgoOrderStore(
                    BinanceUsdmAlgoOrderRepository(session)
                )
                await store.finish(
                    record.client_algo_id,
                    state=BinanceUsdmAlgoOrderState.ACTIVE,
                    result=protection,
                )
                await session.commit()

            async with sessions() as session:
                store = MySqlBinanceUsdmAlgoOrderStore(
                    BinanceUsdmAlgoOrderRepository(session)
                )
                reloaded = await store.load_by_client_algo_id(record.client_algo_id)
            assert reloaded is not None
            assert reloaded.result == protection
            assert reloaded.entry_fill == fill

            # Active protection is still not a licence to place another one.
            async with sessions() as session:
                store = MySqlBinanceUsdmAlgoOrderStore(
                    BinanceUsdmAlgoOrderRepository(session)
                )
                third = await store.prepare(record)
            assert third.acquired is False
            assert third.record.state is BinanceUsdmAlgoOrderState.ACTIVE
        finally:
            await engine.dispose()  # pyright: ignore[reportAttributeAccessIssue]

    asyncio.run(verify())


@pytest.mark.integration
def test_algo_order_store_keeps_the_emergency_close_whole() -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")
    alembic_command.upgrade(Config(ROOT / "alembic.ini"), "head")

    async def verify() -> None:
        engine, sessions = _sessions()
        try:
            binding_id, account_id = await _binding(sessions)
            fill = _fill(binding_id, account_id)
            body = b"algoType=CONDITIONAL&symbol=BTCUSDT&type=STOP_MARKET&x=1"
            client_algo_id = binance_protection_client_algo_id(fill.entry_command_id)
            record = BinanceUsdmAlgoOrderRecord(
                entry_fill=fill,
                client_algo_id=client_algo_id,
                trigger_price=Decimal("109500.30000000000004"),
                request_body=body,
                request_digest=sha256(body).digest(),
                prepared_at=NOW,
                state=BinanceUsdmAlgoOrderState.PREPARED,
                result=None,
            )
            async with sessions() as session:
                store = MySqlBinanceUsdmAlgoOrderStore(
                    BinanceUsdmAlgoOrderRepository(session)
                )
                await store.prepare(record)
                await session.commit()

            # A position that could not be protected was flattened instead,
            # and the close is the evidence of that.
            closed = ProtectionResult(
                provider_algo_id=None,
                client_algo_id=client_algo_id,
                state=BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED,
                trigger_price=record.trigger_price,
                recovered=True,
                emergency_close=_write_result(f"v6-{uuid7().hex}"),
            )
            async with sessions() as session:
                store = MySqlBinanceUsdmAlgoOrderStore(
                    BinanceUsdmAlgoOrderRepository(session)
                )
                await store.finish(
                    client_algo_id,
                    state=BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED,
                    result=closed,
                )
                await session.commit()

            async with sessions() as session:
                store = MySqlBinanceUsdmAlgoOrderStore(
                    BinanceUsdmAlgoOrderRepository(session)
                )
                reloaded = await store.load_by_client_algo_id(client_algo_id)
            assert reloaded is not None
            assert reloaded.result == closed
            assert reloaded.result is not None
            assert reloaded.result.emergency_close == closed.emergency_close
        finally:
            await engine.dispose()  # pyright: ignore[reportAttributeAccessIssue]

    asyncio.run(verify())
