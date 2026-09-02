"""The secrets screen: registering, activating, retiring.

The promise that matters is in section 11.3 — never redisplay plaintext after
the registration POST completes. Registration is the one moment the value
exists in the process, so it is the one moment worth testing hard.
"""

from __future__ import annotations

import asyncio
from base64 import b64encode
from datetime import UTC, datetime
from uuid import uuid7

import httpx
import pytest
from conftest import integration_database_url, integration_redis_url
from fastapi import FastAPI
from redis import asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.app import create_app
from autotrader.apps.backoffice.auth import (
    SESSION_COOKIE,
    BackofficeConfig,
    LoginAttempt,
    Operator,
    Session,
    VerifiedIdentity,
    new_session_id,
)
from autotrader.apps.backoffice.ledger import SOLE_OPERATOR_EMAIL
from autotrader.apps.backoffice.provider_secrets import registerable_secrets
from autotrader.apps.backoffice.second_password import (
    APPROVAL_PREFIX,
    ATTEMPT_PREFIX,
    ApprovalStore,
    MySqlSecondPasswords,
)
from autotrader.apps.backoffice.secrets import (
    OAUTH,
    MySqlSecretStore,
    SecretNotFoundError,
    SecretScope,
)
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.security.secret_crypto import MasterKeyRing

ALLOWED = SOLE_OPERATOR_EMAIL
CSRF = "a-form-token"
BASE_URL = "https://backoffice.example.com"
KEY = b64encode(b"k" * 32).decode()
PASSWORD = "correct horse battery staple"
NAME = "google-oauth-client-secret"
REFERENCE = f"secret://db/{NAME}@active"
VALUE = "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ee-again"
NOW = datetime(2026, 8, 27, tzinfo=UTC)


class _Store:
    def __init__(self) -> None:
        self.sessions: dict[str, Operator] = {}

    async def begin_login(self, attempt: LoginAttempt) -> None:
        raise AssertionError("this scenario never signs in")

    async def take_login(self, state: str) -> LoginAttempt | None:
        raise AssertionError("this scenario never signs in")

    async def create_session(self, operator: Operator) -> str:
        session_id = new_session_id()
        self.sessions[session_id] = operator
        return session_id

    async def session_for(self, session_id: str) -> Session | None:
        operator = self.sessions.get(session_id)
        if operator is None:
            return None
        return Session(operator=operator, csrf_token=CSRF)

    async def end_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)


class _Provider:
    def authorization_url(self, attempt: LoginAttempt) -> str:
        return f"{BASE_URL}/never?state={attempt.state}"

    async def verify(self, *, code: str, attempt: LoginAttempt) -> VerifiedIdentity:
        del code, attempt
        return VerifiedIdentity(email=ALLOWED, email_verified=True)


def _settings(url: str) -> Settings:
    return Settings(
        database_url=url,
        backoffice_public_url=BASE_URL,
        backoffice_master_key=KEY,  # type: ignore[arg-type]
        backoffice_master_key_version=1,
    )


def _keys() -> MasterKeyRing:
    return MasterKeyRing(current_key=b"k" * 32, current_version=1)


def _drive(scenario: object) -> None:
    url = integration_database_url()
    redis_url = integration_redis_url()
    if url is None or redis_url is None:
        pytest.skip("MySQL and Redis are required for acceptance tests")

    async def run() -> None:
        settings = _settings(url)
        engine = create_engine(settings)
        client = redis.from_url(redis_url, decode_responses=True)
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await scenario(settings, sessions, client)  # type: ignore[operator]
        finally:
            for prefix in (APPROVAL_PREFIX, ATTEMPT_PREFIX):
                keys = [key async for key in client.scan_iter(f"{prefix}*")]
                if keys:
                    await client.delete(*keys)
            await client.aclose()
            await engine.dispose()

    asyncio.run(run())


async def _signed_in(
    sessions: async_sessionmaker[AsyncSession], approvals: object
) -> tuple[FastAPI, str]:
    store = _Store()
    app = create_app(
        config=BackofficeConfig(
            public_url=BASE_URL,
            allowed_email=ALLOWED,
            client_id="client",
            client_secret="a-secret-that-must-never-render",
            redis_url="redis://localhost:6379/0",
        ),
        sessions=sessions,
        store=store,
        approvals=ApprovalStore(approvals),  # type: ignore[arg-type]
        provider=_Provider(),
        account_id=uuid7(),
        keys=_keys(),
    )
    return app, await store.create_session(Operator(email=ALLOWED))


def _client(app: FastAPI, session_id: str) -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    )
    client.cookies.set(SESSION_COOKIE, session_id)
    return client


def _approval_id(body: str) -> str:
    marker = 'name="approval_id" value="'
    start = body.index(marker) + len(marker)
    return body[start : body.index('"', start)]


def _register_slot() -> str:
    """The catalogue entry for the secret these tests register.

    Taken from the catalogue rather than written out, because the slot string
    is the form's own encoding of a choice and no operator ever types one.
    What the test is entitled to assert is that this secret can be
    registered, not how the option is spelled.
    """
    for entry in registerable_secrets():
        if entry.logical_name == NAME:
            return entry.slot
    raise AssertionError(f"{NAME} is not registerable")


def _register_form() -> dict[str, str]:
    return {
        "csrf_token": CSRF,
        "slot": _register_slot(),
        "plaintext": VALUE,
    }


@pytest.mark.acceptance
@pytest.mark.integration
def test_registering_never_shows_the_value_back() -> None:
    async def scenario(
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        approvals: object,
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            response = await http.post("/secrets/register", data=_register_form())
            listing = await http.get("/secrets")

        # The one moment the value exists in the process, and it does not
        # come back out.
        assert VALUE not in response.text
        assert VALUE not in listing.text
        assert NAME in response.text

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_registered_secret_is_stored_but_not_yet_in_use() -> None:
    async def scenario(
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        approvals: object,
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            await http.post("/secrets/register", data=_register_form())

        store = MySqlSecretStore(sessions, _keys())
        versions = await store.versions()
        assert [version.version for version in versions] == [1]
        assert versions[0].active is False
        # Storing changes nothing until somebody decides it should.
        with pytest.raises(SecretNotFoundError):
            await store.resolve(REFERENCE)

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_activating_takes_the_password_and_then_a_second_step() -> None:
    async def scenario(
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        approvals: object,
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            await http.post("/secrets/register", data=_register_form())
            approved = await http.post(
                "/secrets/approve",
                data={
                    "csrf_token": CSRF,
                    "action": "ACTIVATE_SECRET",
                    "logical_name": NAME,
                    "target_version": "1",
                    "second_password": PASSWORD,
                },
            )
            await http.post(
                "/secrets/apply",
                data={
                    "csrf_token": CSRF,
                    "action": "ACTIVATE_SECRET",
                    "logical_name": NAME,
                    "target_version": "1",
                    "approval_id": _approval_id(approved.text),
                },
            )

        assert (
            await MySqlSecretStore(sessions, _keys()).resolve(REFERENCE)
        ).reveal() == VALUE

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_wrong_password_never_activates_anything() -> None:
    async def scenario(
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        approvals: object,
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            await http.post("/secrets/register", data=_register_form())
            response = await http.post(
                "/secrets/approve",
                data={
                    "csrf_token": CSRF,
                    "action": "ACTIVATE_SECRET",
                    "logical_name": NAME,
                    "target_version": "1",
                    "second_password": "not the password",
                },
            )

        assert "approval_id" not in response.text
        with pytest.raises(SecretNotFoundError):
            await MySqlSecretStore(sessions, _keys()).resolve(REFERENCE)

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_rotating_activates_the_new_version_and_keeps_the_old_one() -> None:
    async def scenario(
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        approvals: object,
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        store = MySqlSecretStore(sessions, _keys())
        await store.store(
            logical_name=NAME,
            scope=SecretScope(category=OAUTH, provider_code="GOOGLE", environment=None),
            plaintext="the-first-value",
            now=NOW,
        )
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            await http.post("/secrets/register", data=_register_form())
            approved = await http.post(
                "/secrets/approve",
                data={
                    "csrf_token": CSRF,
                    "action": "ACTIVATE_SECRET",
                    "logical_name": NAME,
                    "target_version": "2",
                    "second_password": PASSWORD,
                },
            )
            await http.post(
                "/secrets/apply",
                data={
                    "csrf_token": CSRF,
                    "action": "ACTIVATE_SECRET",
                    "logical_name": NAME,
                    "target_version": "2",
                    "approval_id": _approval_id(approved.text),
                },
            )

        assert (await store.resolve(REFERENCE)).reveal() == VALUE
        # The old version stays on record, so what was in use when is legible.
        assert {version.version for version in await store.versions()} == {1, 2}

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_retiring_leaves_the_name_resolving_to_nothing() -> None:
    async def scenario(
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        approvals: object,
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        store = MySqlSecretStore(sessions, _keys())
        await store.store(
            logical_name=NAME,
            scope=SecretScope(category=OAUTH, provider_code="GOOGLE", environment=None),
            plaintext=VALUE,
            now=NOW,
        )
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            approved = await http.post(
                "/secrets/approve",
                data={
                    "csrf_token": CSRF,
                    "action": "RETIRE_SECRET",
                    "logical_name": NAME,
                    "second_password": PASSWORD,
                },
            )
            await http.post(
                "/secrets/apply",
                data={
                    "csrf_token": CSRF,
                    "action": "RETIRE_SECRET",
                    "logical_name": NAME,
                    "approval_id": _approval_id(approved.text),
                },
            )

        # A refusal, not a stale value.
        with pytest.raises(SecretNotFoundError):
            await store.resolve(REFERENCE)
        # And the version is still there to be read.
        assert len(await store.versions()) == 1

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_listing_shows_what_may_be_shown_and_no_more() -> None:
    async def scenario(
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        approvals: object,
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            await http.post("/secrets/register", data=_register_form())
            body = (await http.get("/secrets")).text

        store = MySqlSecretStore(sessions, _keys())
        version = (await store.versions())[0]
        assert version.fingerprint[:16] in body
        assert version.category in body
        assert "GOOGLE" in body
        assert VALUE not in body

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_page_needs_a_session_like_every_other_one() -> None:
    async def scenario(
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        approvals: object,
    ) -> None:
        app, _ = await _signed_in(sessions, approvals)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            follow_redirects=False,
        ) as http:
            response = await http.get("/secrets")

        assert response.status_code == 303
        assert response.text == ""

    _drive(scenario)
