"""The accounts screen, and what it will not show.

Section 11.2 allows availability and a fingerprint. The test that matters is
the one that stores a credential and then checks the page does not contain it.
"""

from __future__ import annotations

import asyncio
from base64 import b64encode
from datetime import UTC, datetime
from uuid import uuid7

import httpx
import pytest
from conftest import integration_database_url
from fastapi import FastAPI
from integration.risk.test_concurrent_reservation import _seed as _risk_seed
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
from autotrader.apps.backoffice.credentials import store_set
from autotrader.apps.backoffice.provider_secrets import (
    KIS,
    LIVE,
    PAPER,
    TOSS,
    fields_for,
)
from autotrader.apps.backoffice.second_password import ApprovalStore
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.security.secret_crypto import MasterKeyRing

ALLOWED = "operator@example.com"
CSRF = "a-form-token"
BASE_URL = "https://backoffice.example.com"
KEY = b64encode(b"k" * 32).decode()
NOW = datetime(2026, 8, 27, tzinfo=UTC)

KIS_VALUES = {
    "app-key": "a-kis-app-key-nobody-should-see",
    "app-secret": "a-kis-app-secret-nobody-should-see",
    "account-number": "12345678",
    "product-code": "01",
}
TOSS_VALUES = {
    "client-id": "a-toss-client-nobody-should-see",
    "client-secret": "a-toss-secret-nobody-should-see",
}


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


class _NoRedis:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"approvals were consulted: {name}")


def _settings(url: str) -> Settings:
    return Settings(
        database_url=url,
        backoffice_public_url=BASE_URL,
        backoffice_master_key=KEY,  # type: ignore[arg-type]
        backoffice_master_key_version=1,
    )


def _keys() -> MasterKeyRing:
    return MasterKeyRing(current_key=b"k" * 32, current_version=1)


def _app(
    sessions: async_sessionmaker[AsyncSession],
    store: _Store,
    *,
    keys: MasterKeyRing | None,
) -> FastAPI:
    return create_app(
        config=BackofficeConfig(
            public_url=BASE_URL,
            allowed_email=ALLOWED,
            client_id="client",
            client_secret="a-secret-that-must-never-render",
            redis_url="redis://localhost:6379/0",
        ),
        sessions=sessions,
        store=store,
        approvals=ApprovalStore(_NoRedis()),  # type: ignore[arg-type]
        provider=_Provider(),
        account_id=uuid7(),
        keys=keys,
    )


def _drive(scenario: object) -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for acceptance tests")

    async def run() -> None:
        settings = _settings(url)
        engine = create_engine(settings)
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await scenario(settings, sessions)  # type: ignore[operator]
        finally:
            await engine.dispose()

    asyncio.run(run())


async def _page(
    sessions: async_sessionmaker[AsyncSession], *, keys: MasterKeyRing | None = None
) -> httpx.Response:
    store = _Store()
    app = _app(sessions, store, keys=keys or _keys())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    ) as client:
        client.cookies.set(
            SESSION_COOKIE, await store.create_session(Operator(email=ALLOWED))
        )
        return await client.get("/accounts")


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_stored_credential_never_appears_on_the_page() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await store_set(fields_for(KIS, PAPER), KIS_VALUES, settings=settings)
        await store_set(fields_for(TOSS, LIVE), TOSS_VALUES, settings=settings)

        response = await _page(sessions)

        assert response.status_code == 200
        body = response.text
        # Only values long enough for their absence to mean something. A two
        # character product code matches inside a CSS colour, which would make
        # the assertion look strict while proving nothing.
        for value in (*KIS_VALUES.values(), *TOSS_VALUES.values()):
            if len(value) >= 8:
                assert value not in body

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_page_says_which_credentials_are_there() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await store_set(fields_for(KIS, PAPER), KIS_VALUES, settings=settings)

        body = (await _page(sessions)).text

        # Named, counted, and shown as complete.
        assert "kis-paper-app-key" in body
        assert "완비" in body
        assert "(4/4)" in body

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_partial_set_reads_as_incomplete_rather_than_ready() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await store_set(
            fields_for(TOSS, LIVE)[:1],
            {"client-id": TOSS_VALUES["client-id"]},
            settings=settings,
        )

        body = (await _page(sessions)).text

        # An account that cannot sign a request must not read as ready.
        assert "(1/2)" in body
        assert "미완" in body

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_missing_credential_is_a_fact_on_the_page_not_a_failure() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        # Nothing stored at all.
        response = await _page(sessions)

        # A page that failed could not be used to find out what is missing.
        assert response.status_code == 200
        assert "(0/4)" in response.text

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_accounts_the_loop_uses_are_listed() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        ids = await _risk_seed(sessions)
        async with sessions() as session:
            from autotrader.persistence.mysql.models.accounts import Account

            account = await session.get(Account, ids.account_id)
            assert account is not None
            alias = account.account_alias

        body = (await _page(sessions)).text

        assert alias in body

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_without_a_master_key_the_page_refuses_rather_than_reading_empty() -> None:
    """Every credential rendered as absent would look like an empty vault
    instead of a missing key."""

    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await store_set(fields_for(KIS, PAPER), KIS_VALUES, settings=settings)

        store = _Store()
        app = _app(sessions, store, keys=None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as client:
            client.cookies.set(
                SESSION_COOKIE, await store.create_session(Operator(email=ALLOWED))
            )
            response = await client.get("/accounts")

        assert response.status_code == 503

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_page_needs_a_session_like_every_other_one() -> None:
    async def scenario(
        settings: Settings, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        app = _app(sessions, _Store(), keys=_keys())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            follow_redirects=False,
        ) as client:
            response = await client.get("/accounts")

        assert response.status_code == 303
        assert response.text == ""

    _drive(scenario)
