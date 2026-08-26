from __future__ import annotations

import os

import pytest
from redis import asyncio as redis
from sqlalchemy import text

from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine

# CHECK constraints are enforced from MySQL 8.0.16, and the schema relies on
# them heavily. The compose file pins a 8.4 image; this is the floor the code
# actually needs, not the image we happen to deploy.
MINIMUM_MYSQL = (8, 0, 16)
# The event transport is built on streams, which arrived in Redis 5.0.
MINIMUM_REDIS = (5, 0, 0)


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        pytest.skip(f"{name} is required for container integration tests")
    return value


def _version(raw: str) -> tuple[int, ...]:
    digits: list[int] = []
    for part in raw.split("-")[0].split("."):
        if not part.isdigit():
            break
        digits.append(int(part))
    return tuple(digits)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_mysql_session_runtime_policy() -> None:
    """The engine pins UTC, read-committed and strict mode on every session."""
    engine = create_engine(Settings(database_url=required_environment("DATABASE_URL")))
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
        assert _version(row[3]) >= MINIMUM_MYSQL, f"MySQL {row[3]} is too old"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_redis_is_new_enough_for_the_stream_transport() -> None:
    """A server without streams answers PING and then fails every publish."""
    client = redis.from_url(required_environment("REDIS_URL"), decode_responses=True)
    try:
        assert await client.ping() is True
        info = await client.info("server")
        raw = info["redis_version"]
        assert _version(raw) >= MINIMUM_REDIS, f"Redis {raw} has no streams"

        commands = await client.execute_command("COMMAND", "INFO", "XADD", "XAUTOCLAIM")
        assert all(command is not None for command in commands), (
            "the transport needs XADD and XAUTOCLAIM"
        )
    finally:
        await client.aclose()
