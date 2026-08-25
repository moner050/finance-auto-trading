from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine

ROOT = Path(__file__).resolve().parents[3]


def database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if value is None:
        pytest.skip("DATABASE_URL is required for MySQL integration tests")
    return value


def alembic_config() -> Config:
    config = Config(ROOT / "alembic.ini")
    config.attributes["script"] = ScriptDirectory.from_config(config)
    return config


def migration_contracts() -> dict[str, object]:
    contracts: dict[str, object] = {}
    for revision in alembic_config().attributes["script"].walk_revisions():
        contract = getattr(revision.module, "MIGRATION_TEST_CONTRACT", None)
        if not isinstance(contract, dict):
            raise AssertionError(
                f"migration {revision.revision} has no MIGRATION_TEST_CONTRACT"
            )
        contracts[revision.revision] = contract
    return contracts


def table_names(url: str) -> set[str]:
    async def query() -> set[str]:
        engine = create_engine(Settings(database_url=url))
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(
                    lambda sync_connection: set(
                        inspect(sync_connection).get_table_names()
                    )
                )
        finally:
            await engine.dispose()

    return asyncio.run(query())


def column_names(url: str, table_name: str) -> set[str]:
    async def query() -> set[str]:
        engine = create_engine(Settings(database_url=url))
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(
                    lambda sync_connection: {
                        item["name"]
                        for item in inspect(sync_connection).get_columns(table_name)
                    }
                )
        finally:
            await engine.dispose()

    return asyncio.run(query())


def constraint_and_index_names(url: str, table_name: str) -> set[str]:
    async def query() -> set[str]:
        engine = create_engine(Settings(database_url=url))
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(
                    lambda sync_connection: {
                        item["name"]
                        for getter in (
                            inspect(sync_connection).get_check_constraints,
                            inspect(sync_connection).get_foreign_keys,
                            inspect(sync_connection).get_indexes,
                            inspect(sync_connection).get_unique_constraints,
                        )
                        for item in getter(table_name)
                        if item.get("name")
                    }
                )
        finally:
            await engine.dispose()

    return asyncio.run(query())
