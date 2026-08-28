from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid7

import pytest
from alembic import command
from alembic.config import Config
from conftest import integration_database_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.accounts import Broker
from autotrader.persistence.mysql.models.core import (
    CoreExchange,
    CoreInstrument,
    CoreMarket,
)
from autotrader.persistence.mysql.models.positions import Position
from autotrader.persistence.mysql.repositories.accounts import AccountRepository
from autotrader.persistence.mysql.repositories.positions import PositionReader

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 8, 9, tzinfo=UTC)


@pytest.mark.integration
def test_account_isolation_secret_reference_and_signed_observed_position() -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")
    command.upgrade(Config(ROOT / "alembic.ini"), "head")

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            async with session_factory() as session:
                broker = Broker(id=uuid7(), code="TEST", name="Test broker")
                market = CoreMarket(
                    id=uuid7(), code="TT", name="Test market", status="ACTIVE"
                )
                exchange = CoreExchange(
                    id=uuid7(),
                    market_id=market.id,
                    code="TTX",
                    name="Test exchange",
                    status="ACTIVE",
                )
                instrument = CoreInstrument(
                    id=uuid7(),
                    exchange_id=exchange.id,
                    code="TEST1",
                    name="Test instrument",
                    instrument_type="EQUITY",
                    status="ACTIVE",
                )
                session.add_all([broker, market])
                await session.flush()
                session.add(exchange)
                await session.flush()
                session.add(instrument)
                await session.flush()
                accounts = AccountRepository(session)
                paper = await accounts.create(
                    broker_id=broker.id,
                    account_alias="same-alias",
                    environment="PAPER",
                    secret_reference="secret://paper",
                    enabled=True,
                )
                live = await accounts.create(
                    broker_id=broker.id,
                    account_alias="same-alias",
                    environment="LIVE",
                    secret_reference="secret://live",
                    enabled=True,
                )
                assert paper.id != live.id
                with pytest.raises(ValueError, match="plaintext"):
                    await accounts.create(
                        broker_id=broker.id,
                        account_alias="plain-12345678",
                        environment="PAPER",
                        secret_reference="secret://bad",
                        enabled=False,
                    )
                observed = Position(
                    id=uuid7(),
                    account_id=paper.id,
                    instrument_id=instrument.id,
                    quantity=Decimal("-2"),
                    average_cost=Decimal("10"),
                    # A non-zero position states what it is denominated in:
                    # a currency for cash equity, a settlement asset for crypto.
                    currency="USD",
                    settlement_asset=None,
                    observed_at=NOW,
                    blocking_risk=True,
                )
                session.add(observed)
                await session.commit()
            async with session_factory() as session:
                result = await PositionReader(session).get(observed.id)
                assert result is not None
                assert result.quantity == Decimal("-2")
                assert result.blocking_risk is True
                assert not hasattr(PositionReader(session), "update")
        finally:
            await engine.dispose()

    asyncio.run(verify())
