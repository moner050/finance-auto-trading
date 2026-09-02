from __future__ import annotations

from typing import Protocol, cast

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from autotrader.config.settings import Settings


class Cursor(Protocol):
    def execute(self, operation: str) -> object: ...

    def close(self) -> None: ...


class DbApiConnection(Protocol):
    def cursor(self) -> Cursor: ...


def create_engine(settings: Settings) -> AsyncEngine:
    database_url = settings.database_connection_url
    if database_url is None or not database_url.startswith("mysql+aiomysql://"):
        raise ValueError("database URL must use mysql+aiomysql")
    engine = create_async_engine(database_url, pool_pre_ping=True)

    def configure_connection(
        dbapi_connection: object, connection_record: object
    ) -> None:
        del connection_record
        cursor = cast(DbApiConnection, dbapi_connection).cursor()
        cursor.execute("SET time_zone = '+00:00'")
        cursor.execute(
            "SET SESSION sql_mode = "
            "'STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO'"
        )
        # Set here rather than through `execution_options(isolation_level=...)`,
        # which asks SQLAlchemy to set the level on every checkout and put it
        # back on every return. Against a database on the far side of a
        # thirty-millisecond link that costs about 120ms per session - more
        # than the queries themselves on every back office screen. Setting it
        # once when the connection is made gives the same isolation for the
        # life of that connection, and nothing pays for it again.
        cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        cursor.close()

    event.listen(engine.sync_engine, "connect", configure_connection)
    return engine
