"""Establishing the backoffice, against a real MySQL.

Four rows have to become true together, and one of them is the row that says
they did. Half of that standing would be a system that believes it is
configured and cannot sign anybody in.
"""

from __future__ import annotations

import asyncio
from base64 import b64encode

import pytest
from conftest import integration_database_url
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.auth import IdentityUnavailableError
from autotrader.apps.backoffice.bootstrap import (
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    BootstrapInput,
    BootstrapRefusedError,
    already_bootstrapped,
    establish,
    master_key_ring,
)
from autotrader.apps.backoffice.composition import bootstrapped_config
from autotrader.apps.backoffice.second_password import MySqlSecondPasswords
from autotrader.apps.backoffice.secrets import MySqlSecretStore
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.backoffice import (
    BackofficeBootstrapAuthorityRow,
    BackofficeSecondPasswordVersionRow,
    BackofficeSecretVersionRow,
)
from autotrader.security.second_password import verify_second_password

CLIENT_ID = "1234.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-a-client-secret"
PASSWORD = "correct horse battery staple"
KEY = b64encode(b"k" * 32).decode()


def _settings(url: str, **changes: object) -> Settings:
    values: dict[str, object] = {
        "database_url": url,
        "backoffice_public_url": "https://backoffice.example.com",
        "backoffice_allowed_email": "operator@example.com",
        "backoffice_master_key": KEY,
        "backoffice_master_key_version": 1,
        "redis_url": "redis://localhost:6379/0",
    }
    values.update(changes)
    return Settings(**values)  # type: ignore[arg-type]


def _input() -> BootstrapInput:
    return BootstrapInput(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        second_password=PASSWORD,
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
def test_bootstrapping_stores_the_oauth_client_and_the_password() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await establish(_input(), settings=settings)

        store = MySqlSecretStore(sessions, master_key_ring(settings))
        assert (
            await store.resolve(f"secret://db/{GOOGLE_CLIENT_ID}@active")
        ).reveal() == CLIENT_ID
        assert (
            await store.resolve(f"secret://db/{GOOGLE_CLIENT_SECRET}@active")
        ).reveal() == CLIENT_SECRET

        active = await MySqlSecondPasswords(sessions).active()
        assert verify_second_password(active.verifier, PASSWORD)
        # A verifier, not the password.
        assert PASSWORD not in active.verifier

    _drive(scenario)


@pytest.mark.integration
def test_the_authority_row_names_what_was_established() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await establish(_input(), settings=settings)

        async with sessions() as session:
            authority = await session.scalar(select(BackofficeBootstrapAuthorityRow))
            versions = list(
                (await session.scalars(select(BackofficeSecretVersionRow))).all()
            )
            password = await session.scalar(select(BackofficeSecondPasswordVersionRow))

        assert authority is not None and password is not None
        assert authority.scope_key == "PRIMARY"
        assert authority.second_password_version_id == password.id
        assert {version.id for version in versions} == {
            authority.oauth_client_id_secret_id,
            authority.oauth_client_secret_secret_id,
        }
        # It names what was bootstrapped, never what was in it.
        assert len(authority.bootstrap_digest) == 32

    _drive(scenario)


@pytest.mark.integration
def test_bootstrapping_twice_is_refused_rather_than_overwriting() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await establish(_input(), settings=settings)

        # Rotation happens from the authenticated GUI, where it is audited and
        # needs the current password.
        with pytest.raises(BootstrapRefusedError, match="already bootstrapped"):
            await establish(_input(), settings=settings)

        async with sessions() as session:
            assert (
                await session.scalar(
                    select(func.count(BackofficeBootstrapAuthorityRow.id))
                )
            ) == 1

    _drive(scenario)


@pytest.mark.integration
def test_a_failed_bootstrap_leaves_nothing_behind() -> None:
    """The authority row and the secrets are one transaction, so a refusal
    partway cannot leave a half-configured backoffice."""

    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        # A client secret too long for the column fails after the client id
        # has already been written inside the same transaction.
        with pytest.raises(Exception):  # noqa: B017
            await establish(
                BootstrapInput(
                    client_id=CLIENT_ID,
                    client_secret="x" * 20000,
                    second_password=PASSWORD,
                ),
                settings=settings,
            )

        async with sessions() as session:
            assert not await already_bootstrapped(session)
            assert (
                await session.scalar(select(func.count(BackofficeSecretVersionRow.id)))
            ) == 0
            assert (
                await session.scalar(
                    select(func.count(BackofficeSecondPasswordVersionRow.id))
                )
            ) == 0

    _drive(scenario)


@pytest.mark.integration
def test_the_configuration_comes_from_the_database_once_bootstrapped() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await establish(_input(), settings=settings)

        # No OAuth values in the environment at all: the database is the
        # authority now.
        config = await bootstrapped_config(settings, sessions)

        assert config.client_id == CLIENT_ID
        assert config.client_secret == CLIENT_SECRET

    _drive(scenario)


@pytest.mark.integration
def test_before_bootstrap_the_environment_is_still_used() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        from_env = _settings(
            str(settings.database_url),
            oauth_google_client_id="env-client-id",
            oauth_google_client_secret="env-client-secret",
        )

        config = await bootstrapped_config(from_env, sessions)

        assert config.client_id == "env-client-id"

    _drive(scenario)


@pytest.mark.integration
def test_a_bootstrapped_backoffice_never_falls_back_to_the_environment() -> None:
    """Quietly reverting to .env would mean a rotation nobody could tell had
    not taken effect."""

    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await establish(_input(), settings=settings)
        wrong_key = _settings(
            str(settings.database_url),
            backoffice_master_key=b64encode(b"z" * 32).decode(),
            oauth_google_client_id="env-client-id",
            oauth_google_client_secret="env-client-secret",
        )

        # The stored secret cannot be opened with this key, and .env is right
        # there. It is still a refusal.
        with pytest.raises((ValueError, IdentityUnavailableError)):
            await bootstrapped_config(wrong_key, sessions)

    _drive(scenario)
