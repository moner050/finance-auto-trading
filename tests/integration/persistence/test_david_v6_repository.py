from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.david_v6 import DavidV6ManifestRow


@pytest.mark.integration
@pytest.mark.asyncio
async def test_v6_manifest_rows_are_queryable_from_the_migrated_schema() -> None:
    url = os.environ.get("DATABASE_URL")
    if url is None:
        pytest.skip("DATABASE_URL is required for MySQL integration tests")
    engine = create_engine(Settings(database_url=url))
    session_factory = async_sessionmaker(engine)
    try:
        async with session_factory() as session:
            await session.scalars(select(DavidV6ManifestRow).limit(1))
    finally:
        await engine.dispose()
