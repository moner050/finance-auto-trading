"""Secrets against a real MySQL.

The value of this store is what happens when something goes wrong with it: a
ciphertext edited in place, a row copied to another name, a rotation that
half completes. None of those can be checked against a fake.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from conftest import integration_database_url
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.secrets import (
    OAUTH,
    PROVIDER_CREDENTIAL,
    MySqlSecretStore,
    SecretNotFoundError,
    SecretScope,
)
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.backoffice import (
    BackofficeSecretActivationRow,
    BackofficeSecretVersionRow,
)
from autotrader.security.secret_crypto import MasterKeyRing

NOW = datetime(2026, 8, 27, tzinfo=UTC)
NAME = "google-client-secret"
REFERENCE = f"secret://db/{NAME}@active"
OAUTH_SCOPE = SecretScope(category=OAUTH, provider_code="GOOGLE", environment=None)
KIS_SCOPE = SecretScope(
    category=PROVIDER_CREDENTIAL, provider_code="KIS", environment="PAPER"
)


def _keys(*, previous: bool = False) -> MasterKeyRing:
    return MasterKeyRing(
        current_key=b"c" * 32,
        current_version=2,
        previous_key=b"p" * 32 if previous else None,
        previous_version=1 if previous else None,
    )


def _drive(scenario: object) -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")

    async def run() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await scenario(sessions)  # type: ignore[operator]
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.integration
def test_a_stored_secret_comes_back_exactly() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        store = MySqlSecretStore(sessions, _keys())
        await store.store(
            logical_name=NAME, scope=OAUTH_SCOPE, plaintext="GOCSPX-value", now=NOW
        )

        assert (await store.resolve(REFERENCE)).reveal() == "GOCSPX-value"

    _drive(scenario)


@pytest.mark.integration
def test_the_database_never_holds_the_plaintext() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        store = MySqlSecretStore(sessions, _keys())
        await store.store(
            logical_name=NAME, scope=OAUTH_SCOPE, plaintext="GOCSPX-value", now=NOW
        )

        async with sessions() as session:
            row = await session.scalar(select(BackofficeSecretVersionRow))
        assert row is not None
        # A backup carries this and nothing that opens it.
        assert b"GOCSPX-value" not in row.ciphertext
        assert len(row.nonce) == 12
        assert len(row.fingerprint) == 32

    _drive(scenario)


@pytest.mark.integration
def test_rotating_writes_a_new_version_and_moves_the_activation() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        store = MySqlSecretStore(sessions, _keys())
        await store.store(
            logical_name=NAME, scope=OAUTH_SCOPE, plaintext="first", now=NOW
        )
        await store.store(
            logical_name=NAME,
            scope=OAUTH_SCOPE,
            plaintext="second",
            now=NOW + timedelta(days=1),
        )

        assert (await store.resolve(REFERENCE)).reveal() == "second"
        async with sessions() as session:
            versions = await session.scalar(
                select(func.count(BackofficeSecretVersionRow.id))
            )
            active = await session.scalar(
                select(func.count(BackofficeSecretActivationRow.id)).where(
                    BackofficeSecretActivationRow.active_marker == "ACTIVE"
                )
            )
            history = await session.scalar(
                select(func.count(BackofficeSecretActivationRow.id))
            )
        # The old version is still there. What was in use when is readable.
        assert versions == 2
        assert active == 1
        assert history == 2

    _drive(scenario)


@pytest.mark.integration
def test_an_edited_ciphertext_does_not_decrypt_to_anything() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        store = MySqlSecretStore(sessions, _keys())
        await store.store(
            logical_name=NAME, scope=OAUTH_SCOPE, plaintext="GOCSPX-value", now=NOW
        )

        async with sessions() as session:
            row = await session.scalar(select(BackofficeSecretVersionRow))
            assert row is not None
            row.ciphertext = bytes(row.ciphertext[:-1]) + bytes(
                [row.ciphertext[-1] ^ 1]
            )
            await session.commit()

        # Someone with write access to the database still cannot choose what
        # the secret says.
        with pytest.raises(ValueError, match="authentication failed"):
            await store.resolve(REFERENCE)

    _drive(scenario)


@pytest.mark.integration
def test_a_row_cannot_be_moved_to_another_name_at_all() -> None:
    """Two layers, and the schema is the outer one.

    The name is part of the AAD, so a moved ciphertext would not decrypt. It
    never gets that far: the activation carries the name too and the pair is a
    composite foreign key, so the database refuses to let them drift apart.
    """

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        store = MySqlSecretStore(sessions, _keys())
        await store.store(
            logical_name=NAME, scope=OAUTH_SCOPE, plaintext="GOCSPX-value", now=NOW
        )

        with pytest.raises(IntegrityError):
            async with sessions() as session:
                row = await session.scalar(select(BackofficeSecretVersionRow))
                activation = await session.scalar(select(BackofficeSecretActivationRow))
                assert row is not None and activation is not None
                row.logical_name = "another-name"
                activation.logical_name = "another-name"
                await session.commit()

        # And the original still resolves, because nothing moved.
        assert (await store.resolve(REFERENCE)).reveal() == "GOCSPX-value"

    _drive(scenario)


@pytest.mark.integration
def test_a_secret_nobody_stored_is_a_refusal_not_an_empty_value() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        store = MySqlSecretStore(sessions, _keys())

        with pytest.raises(SecretNotFoundError):
            await store.resolve("secret://db/never-stored@active")

    _drive(scenario)


@pytest.mark.integration
def test_a_secret_encrypted_under_the_previous_key_still_opens() -> None:
    """Rotating the master key must not lock the operator out of what is
    already stored."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        old = MasterKeyRing(current_key=b"p" * 32, current_version=1)
        await MySqlSecretStore(sessions, old).store(
            logical_name=NAME, scope=OAUTH_SCOPE, plaintext="GOCSPX-value", now=NOW
        )

        rotated = MySqlSecretStore(sessions, _keys(previous=True))

        assert (await rotated.resolve(REFERENCE)).reveal() == "GOCSPX-value"

    _drive(scenario)


@pytest.mark.integration
def test_a_key_that_never_encrypted_it_cannot_read_it() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        await MySqlSecretStore(sessions, _keys()).store(
            logical_name=NAME, scope=OAUTH_SCOPE, plaintext="GOCSPX-value", now=NOW
        )
        stranger = MySqlSecretStore(
            sessions, MasterKeyRing(current_key=b"x" * 32, current_version=2)
        )

        with pytest.raises(ValueError, match="authentication failed"):
            await stranger.resolve(REFERENCE)

    _drive(scenario)


@pytest.mark.integration
def test_two_providers_keep_separate_secrets() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        store = MySqlSecretStore(sessions, _keys())
        await store.store(
            logical_name="kis-app-key",
            scope=KIS_SCOPE,
            plaintext="kis-value",
            now=NOW,
        )
        await store.store(
            logical_name=NAME, scope=OAUTH_SCOPE, plaintext="oauth-value", now=NOW
        )

        assert (await store.resolve("secret://db/kis-app-key@active")).reveal() == (
            "kis-value"
        )
        assert (await store.resolve(REFERENCE)).reveal() == "oauth-value"

    _drive(scenario)


@pytest.mark.integration
def test_the_fingerprint_is_what_may_be_shown() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        store = MySqlSecretStore(sessions, _keys())
        await store.store(
            logical_name=NAME, scope=OAUTH_SCOPE, plaintext="GOCSPX-value", now=NOW
        )

        fingerprint = await store.fingerprint(REFERENCE)

        # It says which secret this is, not what it says.
        assert len(fingerprint) == 32
        assert b"GOCSPX-value" not in fingerprint

    _drive(scenario)


@pytest.mark.integration
def test_a_rotation_changes_the_fingerprint() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        store = MySqlSecretStore(sessions, _keys())
        await store.store(
            logical_name=NAME, scope=OAUTH_SCOPE, plaintext="first", now=NOW
        )
        before = await store.fingerprint(REFERENCE)
        await store.store(
            logical_name=NAME,
            scope=OAUTH_SCOPE,
            plaintext="second",
            now=NOW + timedelta(days=1),
        )

        # Readiness derived from the old fingerprint is no longer current.
        assert await store.fingerprint(REFERENCE) != before

    _drive(scenario)
