"""Every screen renders through one layout.

Section 12 asks for a persistent navigation rail and a desktop-first layout
with a narrow-screen fallback. Eight templates each carried their own copy of
a style block, so the same table looked different depending on where the
operator had clicked, and only the operations screen linked anywhere.

These render each screen and check the shared frame is actually there. A
template that fails to inherit still returns 200 with its own content, so
nothing else in the suite would notice.
"""

from __future__ import annotations

import asyncio
from base64 import b64encode
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
from autotrader.apps.backoffice.second_password import APPROVAL_PREFIX, ApprovalStore
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.security.secret_crypto import MasterKeyRing

ALLOWED = SOLE_OPERATOR_EMAIL
CSRF = "a-form-token"
BASE_URL = "https://backoffice.example.com"
KEY = b64encode(b"k" * 32).decode()

SCREENS = (
    "/",
    "/accounts",
    "/secrets",
    "/policies",
    "/universe",
    "/evidence",
    "/promotion",
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
            keys = [key async for key in client.scan_iter(f"{APPROVAL_PREFIX}*")]
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
        keys=MasterKeyRing(current_key=b"k" * 32, current_version=1),
    )
    return app, await store.create_session(Operator(email=ALLOWED))


def _client(app: FastAPI, session_id: str) -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    )
    client.cookies.set(SESSION_COOKIE, session_id)
    return client


@pytest.mark.acceptance
@pytest.mark.integration
def test_every_screen_carries_the_navigation_rail() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)
        async with _client(app, session_id) as http:
            for path in SCREENS:
                page = await http.get(path)
                assert page.status_code == 200, path
                assert '<nav class="rail">' in page.text, path
                # Every other screen is reachable from every screen.
                for other in SCREENS:
                    assert f'href="{other}"' in page.text, f"{path} -> {other}"

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_current_screen_is_marked_on_the_rail() -> None:
    """A rail that does not say where you are is a list of links."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)
        async with _client(app, session_id) as http:
            page = await http.get("/policies")

        assert '<a href="/policies" aria-current="page"' in page.text
        # Counted on the rendered anchors only: the stylesheet carries the
        # attribute twice as a selector.
        assert page.text.count('aria-current="page">') == 1

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_style_block_is_defined_once() -> None:
    """Eight copies is how two screens end up disagreeing about a table."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)
        async with _client(app, session_id) as http:
            for path in SCREENS:
                page = await http.get(path)
                assert page.text.count("<style>") == 1, path
                assert page.text.count("<!doctype html>") == 1, path

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_safety_band_is_separate_from_the_ordinary_controls() -> None:
    """Section 12 keeps HALT and EMERGENCY prominent and out of the
    dangerous-action dialog: one click, never the quiet default."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)
        async with _client(app, session_id) as http:
            page = await http.get("/")

        assert '<form class="safety"' in page.text
        assert 'value="HALT"' in page.text
        assert 'value="EMERGENCY"' in page.text
        # Arming is exposure-enabling and never sits beside them as a button.
        assert 'href="/controls/arm"' in page.text


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_second_password_is_asked_for_in_a_dialog() -> None:
    """Section 9 puts it on the step that makes something trade, and it used
    to be a field at the bottom of a long screen - so reaching the one control
    that matters meant scrolling past everything else.

    The form is unchanged: same action, same CSRF, same field. Only where it
    appears is different.
    """

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)
        async with _client(app, session_id) as http:
            for path in SCREENS:
                page = await http.get(path)
                assert page.status_code == 200, path
                if 'name="second_password"' not in page.text:
                    continue
                # Every password field sits inside a dialog, and every dialog
                # has something that opens it.
                assert '<dialog class="confirm"' in page.text, path
                assert "data-opens=" in page.text, path

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_no_two_dialogs_on_a_screen_share_an_identifier() -> None:
    """A dialog id is what the trigger opens. Two rows rendering the same id -
    which is what happens the moment one of these forms is moved inside a
    loop - would make every button open the first row's dialog, and the
    operator would approve something other than what they clicked.
    """
    import re

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)
        async with _client(app, session_id) as http:
            for path in SCREENS:
                page = await http.get(path)
                ids = re.findall(r'<dialog class="confirm" id="([^"]+)"', page.text)
                assert len(ids) == len(set(ids)), f"{path}: duplicate dialog ids {ids}"
                opens = set(re.findall(r'data-opens="([^"]+)"', page.text))
                # Every trigger points at a dialog that is actually on the page.
                assert opens <= set(ids), f"{path}: {opens - set(ids)} open nothing"

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_no_screen_prints_a_utc_timestamp() -> None:
    """One clock for the whole backoffice.

    The store is UTC and the screens are KST, and the danger is not the
    conversion - it is a screen that misses it. A page showing
    `2026-09-02T05:58:30+00:00` beside another showing `2026-09-02 14:58:30`
    gives the operator two times nine hours apart for the same moment, and
    nothing on either page says which is which.

    `+00:00` is the fingerprint: it can only come from a stamp that was
    written out without being converted. The manifest sample on the universe
    screen carries a `+09:00` offset and is meant to.
    """

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)
        async with _client(app, session_id) as http:
            for path in SCREENS:
                page = await http.get(path)
                assert page.status_code == 200, path
                assert "+00:00" not in page.text, f"{path} prints a UTC timestamp"

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_at_most_one_dialog_opens_itself() -> None:
    """A step the server produced opens on load, and exactly one can.

    The script takes the first `data-open` it finds, so two of them on one
    page would mean an operator is shown one confirmation while a second is
    waiting behind it unseen - and both carry a second-password field for a
    different action.
    """
    import re

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)
        async with _client(app, session_id) as http:
            for path in SCREENS:
                page = await http.get(path)
                assert page.status_code == 200, path
                opening = re.findall(
                    r'<dialog class="confirm[^"]*"[^>]*data-open>', page.text
                )
                assert len(opening) <= 1, f"{path} opens {len(opening)} dialogs"

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_dialog_that_opens_itself_says_what_it_is_confirming() -> None:
    """The backdrop hides the page, so anything the operator needs in order
    to answer has to be inside the dialog rather than behind it.

    Before, the facts sat in a panel and the dialog held only the password
    field; opening it dimmed the one thing worth reading.
    """
    import re

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)
        async with _client(app, session_id) as http:
            for path in SCREENS:
                body = (await http.get(path)).text
                for match in re.finditer(
                    r'<dialog class="confirm[^"]*"[^>]*>(.*?)</dialog>', body, re.S
                ):
                    dialog = match.group(1)
                    if 'name="second_password"' not in dialog:
                        continue
                    assert 'class="what"' in dialog, path

    _drive(scenario)
