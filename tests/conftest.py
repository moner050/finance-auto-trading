from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine

ROOT = Path(__file__).resolve().parents[1]


class _MarkerNode(Protocol):
    def get_closest_marker(self, name: str) -> object | None: ...


class _FixtureRequest(Protocol):
    path: Path
    node: _MarkerNode


def _require_disposable_test_database(
    database_url: str,
    *,
    allow_targeted: bool = False,
) -> None:
    url = make_url(database_url)
    local_test_database = (
        url.host in {"127.0.0.1", "localhost"}
        and url.username == "autotrader"
        and url.database == "finance_auto_trading_test"
    )
    if os.environ.get("CI") == "true" and local_test_database:
        return
    if allow_targeted and local_test_database:
        return
    if allow_targeted:
        identity = "|".join(
            (
                url.drivername,
                url.username or "",
                url.host or "",
                str(url.port or ""),
                url.database or "",
            )
        )
        expected = hashlib.sha256(identity.encode()).hexdigest()
        authorized = os.environ.get(
            "AUTOTRADER_AUTHORIZED_TEST_DATABASE_FINGERPRINT",
            "",
        )
        if hmac.compare_digest(authorized, expected):
            return
    raise RuntimeError("schema reset requires the disposable CI test database")


def require_authorized_test_database(database_url: str) -> None:
    _require_disposable_test_database(database_url, allow_targeted=True)


def reset_schema(database_url: str, *, allow_targeted: bool = False) -> None:
    _require_disposable_test_database(database_url, allow_targeted=allow_targeted)

    async def drop_tables() -> None:
        engine = create_engine(Settings(database_url=database_url))
        try:
            async with engine.begin() as connection:
                table_names = (
                    (await connection.execute(sa.text("SHOW TABLES"))).scalars().all()
                )
                await connection.execute(sa.text("SET FOREIGN_KEY_CHECKS = 0"))
                try:
                    for table_name in table_names:
                        identifier = table_name.replace("`", "``")
                        await connection.execute(sa.text(f"DROP TABLE `{identifier}`"))
                finally:
                    await connection.execute(sa.text("SET FOREIGN_KEY_CHECKS = 1"))
        finally:
            await engine.dispose()

    asyncio.run(drop_tables())


def integration_database_url() -> str | None:
    """The database the integration tests run against.

    DATABASE_URL alone was never enough: the application builds its URL from
    the MYSQL_* components too, so a configured .env left every MySQL test
    quietly skipping. Asking Settings is the only way the tests see the same
    database the application does.
    """
    configured = os.environ.get("DATABASE_URL")
    if configured is not None:
        return configured
    return Settings().database_connection_url


def integration_redis_url() -> str | None:
    """The Redis the integration tests run against, resolved as the app does."""
    configured = os.environ.get("REDIS_URL")
    if configured is not None:
        return configured
    return Settings().redis_connection_url


@pytest.fixture(autouse=True)
def reset_integration_database(request: _FixtureRequest) -> None:
    prepare_integration_database(request)


def prepare_integration_database(request: _FixtureRequest) -> None:
    if request.node.get_closest_marker("integration") is None:
        return
    if request.path.parent.name == "migrations":
        return
    database_url = integration_database_url()
    if database_url is None:
        return

    # A non-local database is admitted only by an exact fingerprint match,
    # which is the same authorisation require_authorized_test_database uses.
    require_authorized_test_database(database_url)
    config = Config(ROOT / "alembic.ini")
    if os.environ.get("AUTOTRADER_TEST_SCHEMA_RESET") == "1":
        reset_schema(database_url, allow_targeted=True)
    else:
        command.downgrade(config, "base")
    command.upgrade(config, os.environ.get("AUTOTRADER_TEST_MIGRATION_TARGET", "head"))


@pytest.fixture(autouse=True)
def collect_acceptance_scope(request: _FixtureRequest) -> object:
    evidence_dir = os.environ.get("GATE_EVIDENCE_DIR")
    if request.path.parent.name != "acceptance" or evidence_dir is None:
        yield
        return
    scope_dir = Path(evidence_dir) / "scenarios"
    scopes_before: set[Path] = (
        set(scope_dir.glob("*.scope.json")) if scope_dir.exists() else set()
    )
    yield
    for scope_path in sorted(set(scope_dir.glob("*.scope.json")) - scopes_before):
        subprocess.run(
            (
                sys.executable,
                "scripts/collect-gate-evidence.py",
                "--database-url",
                os.environ["DATABASE_URL"],
                "--scope-dir",
                str(scope_dir),
                "--output-dir",
                str(Path(evidence_dir) / "collected"),
                "--scenario-id",
                scope_path.stem.removesuffix(".scope"),
            ),
            check=True,
            cwd=ROOT,
        )
