"""Starting again, which is the half that has to be hard.

Stopping is covered elsewhere and is deliberately easy. These are the checks
that an operator cannot start trading by accident, by replay, or against a
system that has moved since they looked at it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from acceptance.test_trading_loop import _arm
from conftest import integration_database_url, integration_redis_url
from fastapi import FastAPI
from integration.risk.test_concurrent_reservation import _seed as _risk_seed
from redis import asyncio as redis
from sqlalchemy import select
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
from autotrader.apps.backoffice.exposure import (
    ArmingFacts,
    DangerousAction,
    MySqlExposureControls,
    StillHaltedError,
    approval_for,
    new_exposure_command,
)
from autotrader.apps.backoffice.second_password import (
    APPROVAL_PREFIX,
    ATTEMPT_PREFIX,
    MAX_ATTEMPTS,
    ApprovalRejectedError,
    ApprovalStore,
    MySqlSecondPasswords,
    TooManyAttemptsError,
)
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.operations import (
    OpsAuditLog,
    OpsTradingControl,
)

ALLOWED = "operator@example.com"
CSRF = "a-form-token"
BASE_URL = "https://backoffice.example.com"
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


def _config() -> BackofficeConfig:
    return BackofficeConfig(
        public_url=BASE_URL,
        allowed_email=ALLOWED,
        client_id="client",
        client_secret="a-secret-that-must-never-render",
        redis_url="redis://localhost:6379/0",
    )


def _drive(scenario: object) -> None:
    url = integration_database_url()
    redis_url = integration_redis_url()
    if url is None or redis_url is None:
        pytest.skip("MySQL and Redis are required for acceptance tests")

    async def run() -> None:
        engine = create_engine(Settings(database_url=url))
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


async def _prepare(
    sessions: async_sessionmaker[AsyncSession], client: object
) -> tuple[FastAPI, str, UUID]:
    """A halted system, a password, and a signed-in operator."""
    ids = await _risk_seed(sessions)
    await _arm(sessions, armed=False)
    await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
    store = _Store()
    app = create_app(
        config=_config(),
        sessions=sessions,
        store=store,
        approvals=ApprovalStore(client),  # type: ignore[arg-type]
        provider=_Provider(),
        account_id=ids.account_id,
    )
    return app, await store.create_session(Operator(email=ALLOWED)), ids.account_id


def _client(app: FastAPI, session_id: str) -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE_URL,
        follow_redirects=False,
    )
    client.cookies.set(SESSION_COOKIE, session_id)
    return client


def _approval_id(body: str) -> str:
    marker = 'name="approval_id" value="'
    start = body.index(marker) + len(marker)
    return body[start : body.index('"', start)]


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_panel_names_exactly_what_is_about_to_be_armed() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], client: object
    ) -> None:
        app, session_id, account_id = await _prepare(sessions, client)
        facts = await MySqlExposureControls(
            sessions=sessions,
            approvals=ApprovalStore(client),  # type: ignore[arg-type]
            account_id=account_id,
        ).facts()

        async with _client(app, session_id) as http:
            response = await http.get("/controls/arm")

        assert response.status_code == 200
        # An operator confirms against what is on the screen, so what is on
        # the screen has to be the thing the approval will be bound to.
        assert facts.account_alias in response.text
        assert facts.broker_code in response.text
        assert facts.environment in response.text

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_arming_takes_the_password_and_then_a_second_step() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], client: object
    ) -> None:
        app, session_id, _ = await _prepare(sessions, client)

        async with _client(app, session_id) as http:
            approved = await http.post(
                "/controls/approve",
                data={
                    "action": "ARM",
                    "csrf_token": CSRF,
                    "second_password": PASSWORD,
                },
            )
            assert approved.status_code == 200
            enabled = await http.post(
                "/controls/enable",
                data={
                    "action": "ARM",
                    "csrf_token": CSRF,
                    "approval_id": _approval_id(approved.text),
                },
            )

        assert enabled.status_code == 200
        async with sessions() as session:
            control = await session.scalar(select(OpsTradingControl))
            assert control is not None
            assert control.armed is True
            audit = await session.scalar(
                select(OpsAuditLog).where(OpsAuditLog.action == "BACKOFFICE_ARM")
            )
            assert audit is not None
            assert audit.details["second_password_verified"] is True
            # The password itself is nowhere in the record.
            assert PASSWORD not in str(audit.details)

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_wrong_password_never_produces_an_approval() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], client: object
    ) -> None:
        app, session_id, _ = await _prepare(sessions, client)

        async with _client(app, session_id) as http:
            response = await http.post(
                "/controls/approve",
                data={
                    "action": "ARM",
                    "csrf_token": CSRF,
                    "second_password": "not the password",
                },
            )

        assert response.status_code == 303
        assert "error=password" in response.headers["location"]
        async with sessions() as session:
            control = await session.scalar(select(OpsTradingControl))
            assert control is not None
            assert control.armed is False

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_an_approval_is_spent_once() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], client: object
    ) -> None:
        app, session_id, _ = await _prepare(sessions, client)

        async with _client(app, session_id) as http:
            approved = await http.post(
                "/controls/approve",
                data={
                    "action": "ARM",
                    "csrf_token": CSRF,
                    "second_password": PASSWORD,
                },
            )
            approval_id = _approval_id(approved.text)
            form = {
                "action": "ARM",
                "csrf_token": CSRF,
                "approval_id": approval_id,
            }
            await http.post("/controls/enable", data=form)

            # A resubmitted confirmation is not a second authorization.
            with pytest.raises(ApprovalRejectedError):
                await http.post("/controls/enable", data=form)

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_an_approval_does_not_survive_the_state_it_was_shown_against() -> None:
    """Approve while looking at one control state, then have it change. The
    approval is for what was on the screen and nothing else."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], client: object
    ) -> None:
        app, session_id, _ = await _prepare(sessions, client)

        async with _client(app, session_id) as http:
            approved = await http.post(
                "/controls/approve",
                data={
                    "action": "ARM",
                    "csrf_token": CSRF,
                    "second_password": PASSWORD,
                },
            )
            approval_id = _approval_id(approved.text)

            # Somebody halts in the meantime.
            await http.post("/controls", data={"action": "HALT", "csrf_token": CSRF})

            with pytest.raises(ApprovalRejectedError):
                await http.post(
                    "/controls/enable",
                    data={
                        "action": "ARM",
                        "csrf_token": CSRF,
                        "approval_id": approval_id,
                    },
                )

        async with sessions() as session:
            control = await session.scalar(select(OpsTradingControl))
            assert control is not None
            assert control.armed is False

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_arming_through_a_halt_is_refused() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], client: object
    ) -> None:
        _, session_id, account_id = await _prepare(sessions, client)
        async with sessions() as session:
            control = await session.scalar(select(OpsTradingControl))
            assert control is not None
            control.kill_switch_level = "EMERGENCY"
            await session.commit()

        controls = MySqlExposureControls(
            sessions=sessions,
            approvals=ApprovalStore(client),  # type: ignore[arg-type]
            account_id=account_id,
        )
        facts = await controls.facts()
        approval_id = await ApprovalStore(client).issue(  # type: ignore[arg-type]
            approval_for(
                session_id=session_id,
                operator=Operator(email=ALLOWED),
                action=DangerousAction.ARM,
                facts=facts,
            )
        )

        # A halt that arming could step over would be advisory.
        with pytest.raises(StillHaltedError):
            await controls.apply(
                new_exposure_command(
                    action=DangerousAction.ARM,
                    operator=Operator(email=ALLOWED),
                    source_ip="127.0.0.1",
                    correlation_id="test",
                    approval_id=approval_id,
                    requested_at=NOW,
                ),
                session_id=session_id,
            )

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_clearing_a_halt_does_not_also_arm() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], client: object
    ) -> None:
        app, session_id, _ = await _prepare(sessions, client)
        async with sessions() as session:
            control = await session.scalar(select(OpsTradingControl))
            assert control is not None
            control.kill_switch_level = "BLOCK_NEW_EXPOSURE"
            await session.commit()

        async with _client(app, session_id) as http:
            approved = await http.post(
                "/controls/approve",
                data={
                    "action": "CLEAR_HALT",
                    "csrf_token": CSRF,
                    "second_password": PASSWORD,
                },
            )
            await http.post(
                "/controls/enable",
                data={
                    "action": "CLEAR_HALT",
                    "csrf_token": CSRF,
                    "approval_id": _approval_id(approved.text),
                },
            )

        async with sessions() as session:
            control = await session.scalar(select(OpsTradingControl))
            assert control is not None
            assert control.kill_switch_level == "NONE"
            # Clearing the halt is one decision. Trading again is another.
            assert control.armed is False

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_repeated_wrong_passwords_are_throttled() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], client: object
    ) -> None:
        app, session_id, _ = await _prepare(sessions, client)

        async with _client(app, session_id) as http:
            for _ in range(MAX_ATTEMPTS):
                await http.post(
                    "/controls/approve",
                    data={
                        "action": "ARM",
                        "csrf_token": CSRF,
                        "second_password": "wrong",
                    },
                )

            with pytest.raises(TooManyAttemptsError):
                await http.post(
                    "/controls/approve",
                    data={
                        "action": "ARM",
                        "csrf_token": CSRF,
                        "second_password": PASSWORD,
                    },
                )

    _drive(scenario)


def test_the_panel_facts_are_what_binds_the_approval() -> None:
    facts = ArmingFacts(
        account_alias="an-account",
        broker_code="TEST",
        environment="PAPER",
        policy_version="1",
        armed=False,
        kill_switch_level="NONE",
    )
    moved = ArmingFacts(
        account_alias="an-account",
        broker_code="TEST",
        environment="PAPER",
        policy_version="1",
        armed=False,
        kill_switch_level="BLOCK_NEW_EXPOSURE",
    )

    assert facts.digest() != moved.digest()
