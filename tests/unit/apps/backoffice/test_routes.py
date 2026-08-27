"""What the application does before it knows who is asking.

The database handed to these tests refuses to open a session at all, so any
route that reaches it without an operator fails loudly rather than quietly
serving something.
"""

from __future__ import annotations

from typing import cast
from uuid import uuid7

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.app import LOGIN_PATH, create_app
from autotrader.apps.backoffice.auth import (
    SESSION_COOKIE,
    BackofficeConfig,
    IdentityUnavailableError,
    LoginAttempt,
    Operator,
    Session,
    VerifiedIdentity,
    new_session_id,
)
from autotrader.apps.backoffice.second_password import (
    ApprovalClient,
    ApprovalStore,
)

ALLOWED = "operator@example.com"
CSRF = "a-form-token"


def _approvals() -> ApprovalStore:
    """Redis-backed in the real thing; these tests never reach it."""
    return ApprovalStore(cast("ApprovalClient", _NoRedis()))


class _NoRedis:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"approvals were consulted: {name}")


class _NoDatabase:
    """A session factory that proves it was never called."""

    def __call__(self) -> AsyncSession:
        raise AssertionError("an unauthenticated request reached the database")


class _Store:
    def __init__(self) -> None:
        self.logins: dict[str, LoginAttempt] = {}
        self.sessions: dict[str, Operator] = {}

    async def begin_login(self, attempt: LoginAttempt) -> None:
        self.logins[attempt.state] = attempt

    async def take_login(self, state: str) -> LoginAttempt | None:
        return self.logins.pop(state, None)

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
    def __init__(self, identity: VerifiedIdentity) -> None:
        self._identity = identity
        self.verified: list[str] = []

    def authorization_url(self, attempt: LoginAttempt) -> str:
        return f"https://accounts.example.com/o?state={attempt.state}"

    async def verify(self, *, code: str, attempt: LoginAttempt) -> VerifiedIdentity:
        del attempt
        self.verified.append(code)
        return self._identity


def _config() -> BackofficeConfig:
    return BackofficeConfig(
        public_url="https://backoffice.example.com",
        allowed_email=ALLOWED,
        client_id="client",
        client_secret="secret",
        redis_url="redis://localhost:6379/0",
    )


def _app(store: _Store, provider: _Provider) -> FastAPI:
    return create_app(
        config=_config(),
        sessions=cast("async_sessionmaker[AsyncSession]", _NoDatabase()),
        store=store,
        approvals=_approvals(),
        provider=provider,
        account_id=uuid7(),
    )


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://backoffice.example.com",
        follow_redirects=False,
    )


def _allowed() -> _Provider:
    return _Provider(VerifiedIdentity(email=ALLOWED, email_verified=True))


@pytest.mark.asyncio
async def test_the_dashboard_sends_a_stranger_to_sign_in() -> None:
    async with _client(_app(_Store(), _allowed())) as client:
        response = await client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == LOGIN_PATH


@pytest.mark.asyncio
async def test_a_cookie_the_store_does_not_know_is_not_a_session() -> None:
    """Losing Redis signs everyone out. A cookie is not a second opinion."""
    async with _client(_app(_Store(), _allowed())) as client:
        client.cookies.set(SESSION_COOKIE, new_session_id())
        response = await client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == LOGIN_PATH


@pytest.mark.asyncio
async def test_signing_in_starts_a_one_use_attempt() -> None:
    store = _Store()
    async with _client(_app(store, _allowed())) as client:
        response = await client.get(LOGIN_PATH)

    assert response.status_code == 303
    assert response.headers["location"].startswith("https://accounts.example.com/o?")
    assert len(store.logins) == 1


@pytest.mark.asyncio
async def test_a_callback_admits_the_operator_and_sets_one_cookie() -> None:
    store, provider = _Store(), _allowed()
    async with _client(_app(store, provider)) as client:
        await client.get(LOGIN_PATH)
        state = next(iter(store.logins))

        response = await client.get(f"/auth/callback?code=abc&state={state}")

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert list(store.sessions.values()) == [Operator(email=ALLOWED)]
    # The attempt is spent whatever happened.
    assert store.logins == {}


@pytest.mark.asyncio
async def test_a_replayed_callback_finds_nothing_to_redeem() -> None:
    store, provider = _Store(), _allowed()
    async with _client(_app(store, provider)) as client:
        await client.get(LOGIN_PATH)
        state = next(iter(store.logins))
        await client.get(f"/auth/callback?code=abc&state={state}")

        with pytest.raises(IdentityUnavailableError):
            await client.get(f"/auth/callback?code=abc&state={state}")

    # The provider was asked exactly once, so a replay never even reaches it.
    assert provider.verified == ["abc"]


@pytest.mark.asyncio
async def test_a_callback_with_a_state_nobody_issued_is_refused() -> None:
    store, provider = _Store(), _allowed()
    async with _client(_app(store, provider)) as client:
        with pytest.raises(IdentityUnavailableError):
            await client.get("/auth/callback?code=abc&state=invented")

    assert provider.verified == []
    assert store.sessions == {}


@pytest.mark.asyncio
async def test_another_email_never_gets_a_session() -> None:
    store = _Store()
    provider = _Provider(
        VerifiedIdentity(email="someone@example.com", email_verified=True)
    )
    async with _client(_app(store, provider)) as client:
        await client.get(LOGIN_PATH)
        state = next(iter(store.logins))

        with pytest.raises(IdentityUnavailableError):
            await client.get(f"/auth/callback?code=abc&state={state}")

    assert store.sessions == {}


@pytest.mark.asyncio
async def test_signing_out_ends_the_session_and_clears_the_cookie() -> None:
    store = _Store()
    async with _client(_app(store, _allowed())) as client:
        client.cookies.set(
            SESSION_COOKIE, await store.create_session(Operator(email=ALLOWED))
        )

        response = await client.post("/auth/logout")

    assert response.status_code == 303
    assert response.headers["location"] == LOGIN_PATH
    assert store.sessions == {}


@pytest.mark.asyncio
async def test_health_answers_without_a_session_and_names_nothing() -> None:
    async with _client(_app(_Store(), _allowed())) as client:
        response = await client.get("/healthz")

    # Liveness has to work before anyone signs in, so it must carry nothing.
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_the_application_refuses_to_build_without_a_session_store() -> None:
    with pytest.raises(IdentityUnavailableError, match="store"):
        create_app(
            config=_config(),
            sessions=cast("async_sessionmaker[AsyncSession]", _NoDatabase()),
            store=cast("_Store", None),
            approvals=_approvals(),
            provider=_allowed(),
            account_id=uuid7(),
        )


def test_the_application_refuses_to_build_without_an_identity_provider() -> None:
    with pytest.raises(IdentityUnavailableError, match="provider"):
        create_app(
            config=_config(),
            sessions=cast("async_sessionmaker[AsyncSession]", _NoDatabase()),
            store=_Store(),
            approvals=_approvals(),
            provider=cast("_Provider", None),
            account_id=uuid7(),
        )
