from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.strategy import (
    StrategyDefinition,
    StrategyVersion,
)

FAKE_STRATEGY_ID = UUID("01989400-0000-7000-8000-000000000020")
FAKE_STRATEGY_VERSION_ID = UUID("01989400-0000-7000-8000-000000000021")
FAKE_CONFIGURATION_HASH = hashlib.sha256(b"SYSTEM_FAKE_EXECUTION:v1").digest()


async def seed_research_only_fake_strategy(session: AsyncSession) -> None:
    definition = insert(StrategyDefinition).values(
        id=FAKE_STRATEGY_ID,
        code="SYSTEM_FAKE_EXECUTION",
        research_only=True,
        configuration_hash=FAKE_CONFIGURATION_HASH,
    )
    await session.execute(definition.prefix_with("IGNORE"))
    version = insert(StrategyVersion).values(
        id=FAKE_STRATEGY_VERSION_ID,
        definition_id=FAKE_STRATEGY_ID,
        version="v1",
        status="SHADOW",
        research_only=True,
    )
    await session.execute(version.prefix_with("IGNORE"))
