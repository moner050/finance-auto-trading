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
    engine = create_async_engine(database_url, pool_pre_ping=True).execution_options(
        isolation_level="READ COMMITTED"
    )

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
        cursor.close()

    event.listen(engine.sync_engine, "connect", configure_connection)
    return engine
