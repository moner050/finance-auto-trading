from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid7

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.core import (
    CoreDataSource,
    CoreExchange,
    CoreInstrument,
    CoreMarket,
)
from autotrader.persistence.mysql.seeds.core import (
    BINANCE_USDM_EXCHANGE_ID,
    CRYPTO_MARKET_ID,
    KR_MARKET_ID,
    KRX_EXCHANGE_ID,
    NYSE_EXCHANGE_ID,
    SYSTEM_SOURCE_ID,
    US_MARKET_ID,
    seed_core_reference,
)
from autotrader.persistence.mysql.unit_of_work import SqlAlchemyUnitOfWork

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.integration
def test_core_reference_seed_is_idempotent_and_constraints_hold() -> None:
    url = os.environ.get("DATABASE_URL")
    if url is None:
        pytest.skip("DATABASE_URL is required for MySQL integration tests")
    command.upgrade(Config(ROOT / "alembic.ini"), "head")

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                await seed_core_reference(uow)
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                await seed_core_reference(uow)
            async with session_factory() as session:
                assert await session.scalar(select(func.count(CoreDataSource.id))) == 1
                assert await session.scalar(select(func.count(CoreMarket.id))) == 3
                assert await session.scalar(select(func.count(CoreExchange.id))) == 3
                assert await session.scalar(select(func.count(CoreInstrument.id))) == 0
                assert set(
                    (await session.scalars(select(CoreDataSource.id))).all()
                ) == {SYSTEM_SOURCE_ID}
                assert set((await session.scalars(select(CoreMarket.id))).all()) == {
                    KR_MARKET_ID,
                    US_MARKET_ID,
                    CRYPTO_MARKET_ID,
                }
                assert set((await session.scalars(select(CoreExchange.id))).all()) == {
                    KRX_EXCHANGE_ID,
                    NYSE_EXCHANGE_ID,
                    BINANCE_USDM_EXCHANGE_ID,
                }
                with pytest.raises((IntegrityError, OperationalError)) as error:
                    session.add(CoreMarket(code="XX", name="Invalid", status="INVALID"))
                    await session.flush()
                assert error.value.orig.args[0] == 3819
                await session.rollback()
                with pytest.raises((IntegrityError, OperationalError)) as error:
                    session.add(
                        CoreInstrument(
                            exchange_id=uuid7(),
                            code="INVALID",
                            name="Invalid",
                            instrument_type="EQUITY",
                            status="ACTIVE",
                        )
                    )
                    await session.flush()
                assert error.value.orig.args[0] == 1452
                await session.rollback()
                with pytest.raises((IntegrityError, OperationalError)) as error:
                    session.add(
                        CoreInstrument(
                            exchange_id=KRX_EXCHANGE_ID,
                            code="INVALID_STATUS",
                            name="Invalid status",
                            instrument_type="EQUITY",
                            status="INVALID",
                        )
                    )
                    await session.flush()
                assert error.value.orig.args[0] == 3819
                await session.rollback()
        finally:
            await engine.dispose()

    asyncio.run(verify())
