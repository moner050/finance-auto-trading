from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.strategy import (
    StrategyDefinition,
    StrategyVersion,
)
from autotrader.persistence.mysql.repositories.strategy import StrategyRepository
from autotrader.persistence.mysql.seeds.strategy import seed_research_only_fake_strategy

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.integration
def test_research_only_fake_strategy_is_seeded_and_never_live_approved() -> None:
    url = os.environ.get("DATABASE_URL")
    if url is None:
        pytest.skip("DATABASE_URL is required for MySQL integration tests")
    command.upgrade(Config(ROOT / "alembic.ini"), "head")

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            async with session_factory() as session:
                await seed_research_only_fake_strategy(session)
                await seed_research_only_fake_strategy(session)
                await session.commit()
            async with session_factory() as session:
                definition = await session.scalar(
                    select(StrategyDefinition).where(
                        StrategyDefinition.code == "SYSTEM_FAKE_EXECUTION"
                    )
                )
                assert definition is not None
                assert definition.research_only is True
                version = await session.scalar(
                    select(StrategyVersion).where(
                        StrategyVersion.definition_id == definition.id
                    )
                )
                assert version is not None
                assert version.status == "SHADOW"
                assert version.research_only is True
                with pytest.raises(ValueError, match="research-only"):
                    await StrategyRepository(session).promote_live(version.id)
        finally:
            await engine.dispose()

    asyncio.run(verify())
