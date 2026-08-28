"""Section 11.2's write half: create, enable, disable, bind a provider.

Section 9 gates two of these and not the others, and the reason is worth
testing rather than trusting: a created account cannot trade, so creation has
no gate; a disabled one cannot trade either, so disabling has none; enabling
and binding are what put an account into service, so both do.
"""

from __future__ import annotations

import asyncio
from base64 import b64encode
from datetime import UTC, datetime
from uuid import UUID, uuid7

import httpx
import pytest
from conftest import integration_database_url, integration_redis_url
from fastapi import FastAPI
from redis import asyncio as redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.account_commands import AccountCommandRefusedError
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
from autotrader.apps.backoffice.second_password import (
    APPROVAL_PREFIX,
    ATTEMPT_PREFIX,
    ApprovalStore,
    MySqlSecondPasswords,
)
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.accounts import Account, Broker
from autotrader.persistence.mysql.models.bindings import ProviderAccountBinding
from autotrader.persistence.mysql.repositories.provider_binding import (
    ProviderBindingRefusedError,
)
from autotrader.security.secret_crypto import MasterKeyRing

ALLOWED = SOLE_OPERATOR_EMAIL
CSRF = "a-form-token"
BASE_URL = "https://backoffice.example.com"
KEY = b64encode(b"k" * 32).decode()
PASSWORD = "correct horse battery staple"
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


def _drive(scenario: object) -> None:
    url = integration_database_url()
    redis_url = integration_redis_url()
    if url is None or redis_url is None:
        pytest.skip("MySQL and Redis are required for acceptance tests")

    async def run() -> None:
        engine = create_engine(
            Settings(
                database_url=url,
                backoffice_public_url=BASE_URL,
                backoffice_master_key=KEY,  # type: ignore[arg-type]
                backoffice_master_key_version=1,
            )
        )
        client = redis.from_url(redis_url, decode_responses=True)
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await scenario(sessions, client)  # type: ignore[operator]
        finally:
            for prefix in (APPROVAL_PREFIX, ATTEMPT_PREFIX):
                keys = [key async for key in client.scan_iter(f"{prefix}*")]
                if keys:
                    await client.delete(*keys)
            await client.aclose()
            await engine.dispose()

    asyncio.run(run())


async def _broker(
    sessions: async_sessionmaker[AsyncSession], code: str = "KIS"
) -> UUID:
    async with sessions() as session:
        existing = await session.scalar(select(Broker).where(Broker.code == code))
        if existing is not None:
            broker_id = existing.id
            await session.rollback()
            return broker_id
        broker = Broker(id=uuid7(), code=code, name=f"{code} broker")
        session.add(broker)
        await session.commit()
        return broker.id


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
        keys=MasterKeyRing(current_key=b"k" * 32, current_version=1),
    )
    return app, await store.create_session(Operator(email=ALLOWED))


def _client(app: FastAPI, session_id: str) -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    )
    client.cookies.set(SESSION_COOKIE, session_id)
    return client


def _alias() -> str:
    """Letters only. The repository refuses an alias with six or more digits,
    which is what an account number looks like, and a random hex suffix hits
    that by accident."""
    return "acct-" + "".join(chr(ord("a") + value % 26) for value in uuid7().bytes[:8])


def _approval_id(body: str) -> str:
    marker = 'name="approval_id" value="'
    start = body.index(marker) + len(marker)
    return body[start : body.index('"', start)]


async def _create(
    app: FastAPI, session_id: str, *, alias: str, code: str = "KIS"
) -> UUID:
    async with _client(app, session_id) as http:
        await http.post(
            "/accounts/create",
            data={
                "csrf_token": CSRF,
                "broker_code": code,
                "account_alias": alias,
                "environment": "PAPER",
                "secret_reference": "secret://kis/paper",
            },
        )
    return alias  # type: ignore[return-value]


async def _account(
    sessions: async_sessionmaker[AsyncSession], alias: str
) -> tuple[UUID, bool]:
    async with sessions() as session:
        account = await session.scalar(
            select(Account).where(Account.account_alias == alias)
        )
        assert account is not None
        found = (account.id, account.enabled)
        await session.rollback()
    return found


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_created_account_cannot_trade_yet() -> None:
    """Section 9 gates enablement. A create that enabled would hand out the
    gated state without the gate."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await _broker(sessions)
        app, session_id = await _signed_in(sessions, approvals)
        alias = _alias()

        await _create(app, session_id, alias=alias)

        _, enabled = await _account(sessions, alias)
        assert enabled is False

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_an_account_with_no_provider_cannot_be_enabled() -> None:
    """The loop would pick it up and then fail to act for it."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        await _broker(sessions)
        app, session_id = await _signed_in(sessions, approvals)
        alias = _alias()
        await _create(app, session_id, alias=alias)
        account_id, _ = await _account(sessions, alias)

        async with _client(app, session_id) as http:
            approved = await http.post(
                "/accounts/enable/approve",
                data={
                    "csrf_token": CSRF,
                    "account_id": str(account_id),
                    "second_password": PASSWORD,
                },
            )
            with pytest.raises(AccountCommandRefusedError, match="provider 바인딩"):
                await http.post(
                    "/accounts/enable/apply",
                    data={
                        "csrf_token": CSRF,
                        "account_id": str(account_id),
                        "approval_id": _approval_id(approved.text),
                    },
                )

        _, enabled = await _account(sessions, alias)
        assert enabled is False

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_binding_then_enabling_puts_an_account_into_service() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        await _broker(sessions)
        app, session_id = await _signed_in(sessions, approvals)
        alias = _alias()
        await _create(app, session_id, alias=alias)
        account_id, _ = await _account(sessions, alias)

        async with _client(app, session_id) as http:
            bound = await http.post(
                "/accounts/provider/approve",
                data={
                    "csrf_token": CSRF,
                    "account_id": str(account_id),
                    "provider_code": "KIS",
                    "account_seq": "",
                    "second_password": PASSWORD,
                },
            )
            await http.post(
                "/accounts/provider/apply",
                data={
                    "csrf_token": CSRF,
                    "account_id": str(account_id),
                    "provider_code": "KIS",
                    "account_seq": "",
                    "approval_id": _approval_id(bound.text),
                },
            )
            approved = await http.post(
                "/accounts/enable/approve",
                data={
                    "csrf_token": CSRF,
                    "account_id": str(account_id),
                    "second_password": PASSWORD,
                },
            )
            await http.post(
                "/accounts/enable/apply",
                data={
                    "csrf_token": CSRF,
                    "account_id": str(account_id),
                    "approval_id": _approval_id(approved.text),
                },
            )

        _, enabled = await _account(sessions, alias)
        assert enabled is True

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_disabling_needs_no_second_password() -> None:
    """For the same reason HALT needs none: taking an account out of service
    has to work when the approval path does not."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        await _broker(sessions)
        app, session_id = await _signed_in(sessions, approvals)
        alias = _alias()
        await _create(app, session_id, alias=alias)
        account_id, _ = await _account(sessions, alias)
        async with sessions() as session:
            account = await session.scalar(
                select(Account).where(Account.id == account_id)
            )
            assert account is not None
            account.enabled = True
            await session.commit()

        async with _client(app, session_id) as http:
            await http.post(
                "/accounts/disable",
                data={"csrf_token": CSRF, "account_id": str(account_id)},
            )

        _, enabled = await _account(sessions, alias)
        assert enabled is False

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_rebinding_a_provider_keeps_the_earlier_revision() -> None:
    """A run recorded against revision one has to stay readable after two."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        await _broker(sessions)
        app, session_id = await _signed_in(sessions, approvals)
        alias = _alias()
        await _create(app, session_id, alias=alias)
        account_id, _ = await _account(sessions, alias)

        for _ in range(2):
            async with _client(app, session_id) as http:
                bound = await http.post(
                    "/accounts/provider/approve",
                    data={
                        "csrf_token": CSRF,
                        "account_id": str(account_id),
                        "provider_code": "KIS",
                        "account_seq": "",
                        "second_password": PASSWORD,
                    },
                )
                await http.post(
                    "/accounts/provider/apply",
                    data={
                        "csrf_token": CSRF,
                        "account_id": str(account_id),
                        "provider_code": "KIS",
                        "account_seq": "",
                        "approval_id": _approval_id(bound.text),
                    },
                )

        async with sessions() as session:
            rows = (
                await session.scalars(
                    select(ProviderAccountBinding).where(
                        ProviderAccountBinding.account_id == account_id
                    )
                )
            ).all()
            found = [(row.revision, row.active) for row in rows]
            await session.rollback()

        assert sorted(found) == [(1, False), (2, True)]

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_provider_that_is_not_the_account_broker_is_refused() -> None:
    """The schema keys the binding to (broker, provider); a mismatch names a
    pair no row has."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        await _broker(sessions)
        app, session_id = await _signed_in(sessions, approvals)
        alias = _alias()
        await _create(app, session_id, alias=alias)
        account_id, _ = await _account(sessions, alias)

        async with _client(app, session_id) as http:
            bound = await http.post(
                "/accounts/provider/approve",
                data={
                    "csrf_token": CSRF,
                    "account_id": str(account_id),
                    "provider_code": "TOSS",
                    "account_seq": "7",
                    "second_password": PASSWORD,
                },
            )
            with pytest.raises(ProviderBindingRefusedError, match="브로커는"):
                await http.post(
                    "/accounts/provider/apply",
                    data={
                        "csrf_token": CSRF,
                        "account_id": str(account_id),
                        "provider_code": "TOSS",
                        "account_seq": "7",
                        "approval_id": _approval_id(bound.text),
                    },
                )

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_an_alias_that_looks_like_an_account_number_is_refused() -> None:
    """Section 11.2 shows aliases, not account numbers."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await _broker(sessions)
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            with pytest.raises(ValueError, match="plaintext account number"):
                await http.post(
                    "/accounts/create",
                    data={
                        "csrf_token": CSRF,
                        "broker_code": "KIS",
                        "account_alias": "50123456-01",
                        "environment": "PAPER",
                        "secret_reference": "secret://kis/paper",
                    },
                )

    _drive(scenario)
