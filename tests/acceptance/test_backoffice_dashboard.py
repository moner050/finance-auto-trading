"""What an operator sees after the loop has run.

The projection tests prove the queries answer correctly. This proves the
answer reaches a page, that the page is reachable only with a session, and
that what lands in the HTML is what the loop actually did rather than a
hopeful summary of it.
"""

from __future__ import annotations

import asyncio
import re
from typing import cast
from uuid import UUID, uuid7

import httpx
import pytest
from acceptance.test_trading_loop import (
    _arm,
    _Bars,
    _context,
    _ports,
    _register_strategy,
)
from conftest import integration_database_url
from fastapi import FastAPI
from integration.apps.test_trader_tick import NOW
from integration.risk.test_concurrent_reservation import _seed as _risk_seed
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.app import LOGIN_PATH, create_app
from autotrader.apps.backoffice.auth import (
    SESSION_COOKIE,
    BackofficeConfig,
    CsrfRejectedError,
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
from autotrader.apps.trader.loop import run_pass
from autotrader.apps.trader.market_data import HLIT_TIMEFRAME
from autotrader.apps.trader.tick import DISARMED, SUBMITTED
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.core import CoreInstrument
from autotrader.persistence.mysql.models.operations import (
    OpsAuditLog,
    OpsTradingControl,
)

# The command table pins the operator in a CHECK constraint, so a
# command from anyone else is refused by the database itself.
ALLOWED = "operator@example.com"
CSRF = "a-form-token"


def _approvals() -> ApprovalStore:
    """Redis-backed in the real thing; these tests never reach it."""
    return ApprovalStore(cast("ApprovalClient", _NoRedis()))


class _NoRedis:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"approvals were consulted: {name}")


BASE_URL = "https://backoffice.example.com"

# Anything shaped like a credential has no business in a rendered page.
_FORBIDDEN = re.compile(
    r"client_secret|refresh_token|access_token|BEGIN [A-Z ]*PRIVATE KEY",
    re.IGNORECASE,
)


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


def _backoffice(
    sessions: async_sessionmaker[AsyncSession], store: _Store, account_id: UUID
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
        approvals=_approvals(),
        provider=_Provider(),
        account_id=account_id,
    )


def _drive(scenario: object) -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for acceptance tests")

    async def run() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await scenario(sessions)  # type: ignore[operator]
        finally:
            await engine.dispose()

    asyncio.run(run())


async def _run_one_entry(
    sessions: async_sessionmaker[AsyncSession], ids: object
) -> None:
    """Take one bar through the loop so there is something to look at."""
    manifest = await _register_strategy(sessions, uuid7())
    bars = _Bars()
    ports = _ports(
        sessions,
        context=_context(manifest, ids.instrument_id),  # type: ignore[attr-defined]
        ids=ids,
        bars=bars,
        lease_name=f"backoffice:{uuid7().hex[:12]}",
    )
    await run_pass(now=NOW, ports=ports)
    bars.closed = True
    await run_pass(now=NOW + HLIT_TIMEFRAME, ports=ports)


@pytest.mark.acceptance
@pytest.mark.integration
def test_an_operator_sees_the_position_the_loop_opened() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ids = await _risk_seed(sessions)
        await _arm(sessions, armed=True)
        await _run_one_entry(sessions, ids)
        async with sessions() as session:
            code = await session.scalar(
                select(CoreInstrument.code).where(
                    CoreInstrument.id == ids.instrument_id  # type: ignore[attr-defined]
                )
            )

        store = _Store()
        app = _backoffice(sessions, store, ids.account_id)  # type: ignore[attr-defined]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as client:
            client.cookies.set(
                SESSION_COOKIE, await store.create_session(Operator(email=ALLOWED))
            )
            response = await client.get("/")

        assert response.status_code == 200
        body = response.text
        assert code is not None and code in body
        # The stop the loop placed shows as protection rather than as absence.
        assert "손절 있음" in body
        assert ALLOWED in body

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_screen_says_stopped_when_a_kill_switch_is_down() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ids = await _risk_seed(sessions)
        await _arm(sessions, armed=True)
        await _run_one_entry(sessions, ids)
        async with sessions() as session:
            control = await session.scalar(select(OpsTradingControl))
            assert control is not None
            control.kill_switch_level = "BLOCK_NEW_EXPOSURE"
            await session.commit()

        store = _Store()
        app = _backoffice(sessions, store, ids.account_id)  # type: ignore[attr-defined]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as client:
            client.cookies.set(
                SESSION_COOKIE, await store.create_session(Operator(email=ALLOWED))
            )
            response = await client.get("/")

        # The armed flag is still true. The loop refuses anyway, and reading
        # "running" off this page would be reading the wrong thing.
        assert "DISARMED" in response.text
        assert "BLOCK_NEW_EXPOSURE" in response.text

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_rendered_page_carries_nothing_that_looks_like_a_credential() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ids = await _risk_seed(sessions)
        await _arm(sessions, armed=True)
        await _run_one_entry(sessions, ids)

        store = _Store()
        app = _backoffice(sessions, store, ids.account_id)  # type: ignore[attr-defined]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as client:
            client.cookies.set(
                SESSION_COOKIE, await store.create_session(Operator(email=ALLOWED))
            )
            response = await client.get("/")

        assert _FORBIDDEN.search(response.text) is None
        # The application's own client secret was in reach of the template.
        assert "a-secret-that-must-never-render" not in response.text

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_same_page_without_a_session_shows_nothing_at_all() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ids = await _risk_seed(sessions)
        await _arm(sessions, armed=True)
        await _run_one_entry(sessions, ids)

        app = _backoffice(sessions, _Store(), ids.account_id)  # type: ignore[attr-defined]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            follow_redirects=False,
        ) as client:
            response = await client.get("/")

        assert response.status_code == 303
        assert response.headers["location"] == LOGIN_PATH
        # Not a redacted page. No page.
        assert response.text == ""

    _drive(scenario)


async def _signed_in(
    sessions: async_sessionmaker[AsyncSession], store: _Store, account_id: UUID
) -> tuple[FastAPI, str]:
    app = _backoffice(sessions, store, account_id)
    return app, await store.create_session(Operator(email=ALLOWED))


@pytest.mark.acceptance
@pytest.mark.integration
def test_halting_from_the_screen_stops_the_running_loop() -> None:
    """The completion criterion for this phase: a loop that is taking bars
    stops because somebody pressed a button."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ids = await _risk_seed(sessions)
        await _arm(sessions, armed=True)
        manifest = await _register_strategy(sessions, uuid7())
        bars = _Bars()
        ports = _ports(
            sessions,
            context=_context(manifest, ids.instrument_id),  # type: ignore[attr-defined]
            ids=ids,
            bars=bars,
            lease_name=f"halt:{uuid7().hex[:12]}",
        )
        assert (await run_pass(now=NOW, ports=ports)).reason == SUBMITTED

        app, session_id = await _signed_in(sessions, _Store(), ids.account_id)  # type: ignore[attr-defined]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as client:
            client.cookies.set(SESSION_COOKIE, session_id)
            response = await client.post(
                "/controls", data={"action": "HALT", "csrf_token": CSRF}
            )

        assert response.status_code == 200
        assert "DISARMED" in response.text
        assert "BLOCK_NEW_EXPOSURE" in response.text

        # A fresh bar to evaluate, so the refusal is the armed check rather
        # than the absence of anything to do. Nothing was restarted or told to
        # reload: the loop reads the control every pass.
        bars.closed = True
        resumed = _ports(
            sessions,
            context=_context(manifest, ids.instrument_id),  # type: ignore[attr-defined]
            ids=ids,
            bars=bars,
            lease_name=f"halt:{uuid7().hex[:12]}",
        )
        after = await run_pass(now=NOW + HLIT_TIMEFRAME, ports=resumed)
        assert after.reason == DISARMED

        async with sessions() as session:
            audit = await session.scalar(
                select(OpsAuditLog).where(OpsAuditLog.action == "BACKOFFICE_HALT")
            )
            assert audit is not None
            assert audit.details["operator_email"] == ALLOWED
            assert audit.details["before"] == {
                "armed": True,
                "kill_switch_level": "NONE",
            }
            assert audit.details["after"] == {
                "armed": False,
                "kill_switch_level": "BLOCK_NEW_EXPOSURE",
            }

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_form_without_the_session_token_changes_nothing() -> None:
    """A cross-site form submits no token. That is the whole attack, and it
    has to fail before anything is written."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ids = await _risk_seed(sessions)
        await _arm(sessions, armed=True)

        app, session_id = await _signed_in(sessions, _Store(), ids.account_id)  # type: ignore[attr-defined]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as client:
            client.cookies.set(SESSION_COOKIE, session_id)
            with pytest.raises(CsrfRejectedError):
                await client.post(
                    "/controls",
                    data={"action": "HALT", "csrf_token": "borrowed-from-nowhere"},
                )

        async with sessions() as session:
            control = await session.scalar(select(OpsTradingControl))
            assert control is not None
            assert control.armed is True
            assert control.kill_switch_level == "NONE"
            assert await session.scalar(select(OpsAuditLog)) is None

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_halting_without_a_session_is_refused_before_anything_is_read() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ids = await _risk_seed(sessions)
        await _arm(sessions, armed=True)

        app = _backoffice(sessions, _Store(), ids.account_id)  # type: ignore[attr-defined]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                "/controls", data={"action": "HALT", "csrf_token": CSRF}
            )

        assert response.status_code == 303
        assert response.headers["location"] == LOGIN_PATH
        async with sessions() as session:
            control = await session.scalar(select(OpsTradingControl))
            assert control is not None
            assert control.armed is True

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_resubmitted_form_is_the_same_command_not_a_second_one() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ids = await _risk_seed(sessions)
        await _arm(sessions, armed=True)

        app, session_id = await _signed_in(sessions, _Store(), ids.account_id)  # type: ignore[attr-defined]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as client:
            client.cookies.set(SESSION_COOKIE, session_id)
            for _ in range(3):
                await client.post(
                    "/controls", data={"action": "DISARM", "csrf_token": CSRF}
                )

        async with sessions() as session:
            entries = (await session.scalars(select(OpsAuditLog))).all()

        # Three presses, three commands, because each carries its own id. What
        # must not happen is a control that ends up in a different state for
        # having been pressed more than once.
        assert len(entries) == 3
        assert all(
            entry.details["after"] == {"armed": False, "kill_switch_level": "NONE"}
            for entry in entries
        )

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_screen_draws_the_day_including_the_hours_it_was_down() -> None:
    """A read model that nobody renders is the defect this codebase keeps
    producing, so the assertion is on the page rather than on the projection.

    Twenty-four columns whatever happened: the operator's first question on
    arriving is whether the loop ran all night, and a chart that only draws
    the busy hours answers yes every time.
    """
    import re

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ids = await _risk_seed(sessions)
        await _arm(sessions, armed=True)

        store = _Store()
        app = _backoffice(sessions, store, ids.account_id)  # type: ignore[attr-defined]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as client:
            client.cookies.set(
                SESSION_COOKIE, await store.create_session(Operator(email=ALLOWED))
            )
            response = await client.get("/")

        assert response.status_code == 200
        body = response.text
        assert "최근 24시간" in body
        chart = body[
            body.index('<ul class="series">') : body.index('<ul class="axis">')
        ]
        assert chart.count("<li") == 24, "every hour gets a column"
        # Heights are a percentage of the tallest hour and are printed into
        # the style attribute, so they have to be there and be in range.
        heights = [int(value) for value in re.findall(r"height: (\d+)%", chart)]
        assert len(heights) == 24
        assert all(0 <= height <= 100 for height in heights)

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_decision_list_names_the_instrument_and_reads_its_codes() -> None:
    """The screen used to say a verdict and nothing about what it was a
    verdict on: no symbol, no venue, no indicator the engine actually found -
    only the blockers, as bare identifiers.

    All four are recorded. The reading is asserted alongside the code because
    section 12 asks for both, and because a table that silently dropped its
    labels would look exactly like one whose codes had all been renamed.
    """

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        ids = await _risk_seed(sessions)
        await _arm(sessions, armed=True)
        await _run_one_entry(sessions, ids)
        async with sessions() as session:
            code = await session.scalar(
                select(CoreInstrument.code).where(
                    CoreInstrument.id == ids.instrument_id  # type: ignore[attr-defined]
                )
            )

        store = _Store()
        app = _backoffice(sessions, store, ids.account_id)  # type: ignore[attr-defined]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as client:
            client.cookies.set(
                SESSION_COOKIE, await store.create_session(Operator(email=ALLOWED))
            )
            body = (await client.get("/")).text

        assert code is not None and code in body
        decisions = body[body.index('id="recent-decisions"') :]
        # Whatever the market decided that pass, the row says what it was
        # about and in whose language.
        assert "종목" in decisions and "거래소" in decisions
        for code_and_label in (
            ("SETUP_REJECTED", "셋업 등급 미달"),
            ("ROUNDED_QUANTITY_ZERO", "수량이 0으로 내림"),
        ):
            stored, label = code_and_label
            if stored in decisions:
                assert label in decisions, stored

    _drive(scenario)
