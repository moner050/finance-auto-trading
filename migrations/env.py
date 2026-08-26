from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models import metadata

config = context.config
# Bound so autogenerate can report drift between the ORM and the schema.
# Leaving this at None is what let the two diverge unnoticed before.
target_metadata = metadata


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_engine(Settings())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


run_migrations_online()
