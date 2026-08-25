from __future__ import annotations

import os

import pytest
from redis import asyncio as redis
from sqlalchemy import text

from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        pytest.skip(f"{name} is required for container integration tests")
    return value


@pytest.mark.asyncio
@pytest.mark.integration
async def test_mysql_redis_container_runtime_policy() -> None:
    engine = create_engine(Settings(database_url=required_environment("DATABASE_URL")))
    client = redis.from_url(required_environment("REDIS_URL"))
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT @@session.time_zone, @@transaction_isolation, "
                        "@@session.sql_mode, VERSION()"
                    )
                )
            ).one()

        assert row[0] == "+00:00"
        assert row[1] == "READ-COMMITTED"
        assert row[2] == (
            "STRICT_ALL_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO"
        )
        assert row[3].startswith("8.4.11")
        assert await client.ping() is True
    finally:
        await client.aclose()
        await engine.dispose()
