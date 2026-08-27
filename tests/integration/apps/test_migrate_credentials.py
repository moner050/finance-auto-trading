"""Moving credentials out of a file and into the database."""

from __future__ import annotations

import asyncio
from base64 import b64encode
from pathlib import Path

import pytest
from conftest import integration_database_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.bootstrap import master_key_ring
from autotrader.apps.backoffice.migrate_credentials import (
    MigrationRefusedError,
    migrate,
    read_from_dotenv,
)
from autotrader.apps.backoffice.provider_secrets import (
    KIS,
    KIS_PAPER_REFERENCE,
    LIVE,
    PAPER,
    TOSS,
    TOSS_LIVE_REFERENCE,
    MySqlAccountSecretResolver,
)
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine

KEY = b64encode(b"k" * 32).decode()

KIS_PAPER_ENV = """KIS_PAPER_APP_KEY=a-paper-app-key
KIS_PAPER_SECRET_KEY=a-paper-app-secret
KIS_PAPER_BANK_NO=12345678
KIS_PAPER_ACCOUNT_PRODUCT_CODE=01
"""
TOSS_ENV = """TOSS_CLIENT_ID=a-toss-client
TOSS_CLIENT_SECRET=a-toss-secret
"""


def _settings(url: str) -> Settings:
    return Settings(
        database_url=url,
        backoffice_public_url="https://backoffice.example.com",
        backoffice_master_key=KEY,  # type: ignore[arg-type]
        backoffice_master_key_version=1,
    )


def _drive(scenario: object) -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")

    async def run() -> None:
        settings = _settings(url)
        engine = create_engine(settings)
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await scenario(settings, sessions)  # type: ignore[operator]
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.integration
def test_a_kis_paper_set_moves_across_unchanged(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(KIS_PAPER_ENV, encoding="utf-8")

    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await migrate(KIS, PAPER, env_file, settings=settings)

        secret = await MySqlAccountSecretResolver(
            sessions, master_key_ring(settings)
        ).resolve_kis(KIS_PAPER_REFERENCE)
        assert secret.app_key.get_secret_value() == "a-paper-app-key"
        assert secret.account_number.get_secret_value() == "12345678"
        assert secret.product_code == "01"

    _drive(scenario)


@pytest.mark.integration
def test_a_toss_set_moves_across_unchanged(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(TOSS_ENV, encoding="utf-8")

    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await migrate(TOSS, LIVE, env_file, settings=settings)

        secret = await MySqlAccountSecretResolver(
            sessions, master_key_ring(settings)
        ).resolve_toss(TOSS_LIVE_REFERENCE)
        assert secret.client_secret.get_secret_value() == "a-toss-secret"

    _drive(scenario)


@pytest.mark.integration
def test_the_file_still_holds_the_values_afterwards(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(TOSS_ENV, encoding="utf-8")

    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await migrate(TOSS, LIVE, env_file, settings=settings)

        # Deleting the only copy of a credential the moment before something
        # goes wrong is not this tool's job.
        assert env_file.read_text(encoding="utf-8") == TOSS_ENV

    _drive(scenario)


def test_a_partial_file_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("KIS_PAPER_APP_KEY=only-one-value\n", encoding="utf-8")

    with pytest.raises(MigrationRefusedError, match="complete KIS PAPER set"):
        read_from_dotenv(KIS, PAPER, env_file)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MigrationRefusedError):
        read_from_dotenv(TOSS, LIVE, tmp_path / "absent")


def test_the_refusal_never_names_which_value_was_missing(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TOSS_CLIENT_ID=a-toss-client\n", encoding="utf-8")

    with pytest.raises(MigrationRefusedError) as caught:
        read_from_dotenv(TOSS, LIVE, env_file)

    assert "CLIENT_SECRET" not in str(caught.value)
