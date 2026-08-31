"""Section 11.5, against section 17's constraint.

The screen uploads a published list, compares it with the one in force, and
activates it behind the second password. What it must not have is a way to
change one symbol, and what it must not lose is the list it replaced.

So the tests that matter are: an upload that does not verify never reaches the
table, activation demands the password, and after the change the old list is
still there to answer a question about the day it was in force.
"""

from __future__ import annotations

import asyncio
import json
from base64 import b64encode
from datetime import UTC, date, datetime
from uuid import UUID, uuid7

import httpx
import pytest
from conftest import integration_database_url, integration_redis_url
from fastapi import FastAPI
from redis import asyncio as redis
from sqlalchemy import delete, select
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
from autotrader.apps.backoffice.second_password import (
    APPROVAL_PREFIX,
    ATTEMPT_PREFIX,
    ApprovalStore,
    MySqlSecondPasswords,
)
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.backoffice import BackofficeCommandRow
from autotrader.persistence.mysql.models.universe import (
    UniverseSnapshotMemberRow,
    UniverseSnapshotRow,
)
from autotrader.persistence.mysql.repositories.universe import UniverseAuthorities
from autotrader.security.secret_crypto import MasterKeyRing

ALLOWED = SOLE_OPERATOR_EMAIL
CSRF = "a-form-token"
BASE_URL = "https://backoffice.example.com"
KEY = b64encode(b"k" * 32).decode()
PASSWORD = "a second password that is long enough"
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
AUGUST = date(2026, 8, 31)
SEPTEMBER = date(2026, 9, 30)


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
            async with sessions() as session:
                await session.execute(delete(UniverseSnapshotMemberRow))
                await session.execute(delete(UniverseSnapshotRow))
                await session.commit()
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
        keys=MasterKeyRing(current_key=b"k" * 32, current_version=1),
    )
    return app, await store.create_session(Operator(email=ALLOWED))


def _client(app: FastAPI, session_id: str) -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    )
    client.cookies.set(SESSION_COOKIE, session_id)
    return client


def _document(
    *,
    effective_date: date = AUGUST,
    members: list[dict[str, object]] | None = None,
) -> bytes:
    return json.dumps(
        {
            "universe_code": "KOSPI200",
            "effective_date": effective_date.isoformat(),
            "source": {
                "name": "KRX",
                "reference": f"kospi200-{effective_date.isoformat()}",
                "published_at": "2026-08-30T09:00:00+00:00",
            },
            "members": members
            or [
                {"symbol": "005930", "common_stock": True, "sector": "IT"},
                {"symbol": "000660", "common_stock": True, "sector": "IT"},
            ],
        }
    ).encode("utf-8")


def _upload(document: bytes, *, digest: str = "") -> dict[str, object]:
    return {
        "files": {"manifest": ("universe.json", document, "application/json")},
        "data": {"csrf_token": CSRF, "claimed_digest": digest},
    }


def _approval_id(body: str) -> str:
    marker = 'name="approval_id" value="'
    start = body.index(marker) + len(marker)
    return body[start : body.index('"', start)]


async def _snapshot_ids(
    sessions: async_sessionmaker[AsyncSession],
) -> list[UUID]:
    async with sessions() as session:
        rows = await UniverseAuthorities(session).history("KOSPI200")
        return [row.id for row in rows]


@pytest.mark.acceptance
@pytest.mark.integration
def test_an_uploaded_list_is_staged_and_not_yet_in_force() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            page = await http.post("/universe/stage", **_upload(_document()))

        assert page.status_code == 200
        async with sessions() as session:
            repository = UniverseAuthorities(session)
            assert await repository.active("KOSPI200") is None
            assert len(await repository.history("KOSPI200")) == 1

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_manifest_that_does_not_match_its_digest_never_reaches_the_table() -> None:
    """The operator's digest and ours agreeing is the only evidence that the
    file they hold and the file we received are the same file."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            page = await http.post(
                "/universe/stage", **_upload(_document(), digest="00" * 32)
            )

        assert "다이제스트" in page.text or "digest" in page.text
        assert await _snapshot_ids(sessions) == []

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_member_without_a_share_class_is_refused_at_upload() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            await http.post(
                "/universe/stage",
                **_upload(_document(members=[{"symbol": "005930", "sector": "IT"}])),
            )

        assert await _snapshot_ids(sessions) == []

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_activation_without_the_password_only_shows_what_would_change() -> None:
    """The first press is the panel. Nothing moves until the password is
    typed against the symbols it names."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            await http.post("/universe/stage", **_upload(_document()))
            snapshot_id = (await _snapshot_ids(sessions))[0]
            panel = await http.post(
                "/universe/approve",
                data={"csrf_token": CSRF, "snapshot_id": str(snapshot_id)},
            )

        assert "005930" in panel.text
        assert "2차 비밀번호" in panel.text
        async with sessions() as session:
            assert await UniverseAuthorities(session).active("KOSPI200") is None

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_approved_snapshot_becomes_the_authority() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            await http.post("/universe/stage", **_upload(_document()))
            snapshot_id = (await _snapshot_ids(sessions))[0]
            approved = await http.post(
                "/universe/approve",
                data={
                    "csrf_token": CSRF,
                    "snapshot_id": str(snapshot_id),
                    "second_password": PASSWORD,
                },
            )
            await http.post(
                "/universe/activate",
                data={
                    "csrf_token": CSRF,
                    "snapshot_id": str(snapshot_id),
                    "approval_id": _approval_id(approved.text),
                },
            )

        async with sessions() as session:
            active = await UniverseAuthorities(session).active("KOSPI200")

        assert active is not None
        assert active.id == snapshot_id
        assert active.activated_by == ALLOWED

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_wrong_password_leaves_the_authority_alone() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            await http.post("/universe/stage", **_upload(_document()))
            snapshot_id = (await _snapshot_ids(sessions))[0]
            refused = await http.post(
                "/universe/approve",
                data={
                    "csrf_token": CSRF,
                    "snapshot_id": str(snapshot_id),
                    "second_password": "not the password",
                },
            )

        assert 'name="approval_id"' not in refused.text
        async with sessions() as session:
            assert await UniverseAuthorities(session).active("KOSPI200") is None

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_replaced_list_still_answers_about_the_day_it_governed() -> None:
    """The point of the whole screen. After September takes over, an August
    decision is still filtered by August's list."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            for document in (
                _document(),
                _document(
                    effective_date=SEPTEMBER,
                    members=[
                        {"symbol": "000660", "common_stock": True, "sector": "IT"}
                    ],
                ),
            ):
                await http.post("/universe/stage", **_upload(document))
            # Oldest first, so August is in force before September replaces it.
            for snapshot_id in reversed(await _snapshot_ids(sessions)):
                approved = await http.post(
                    "/universe/approve",
                    data={
                        "csrf_token": CSRF,
                        "snapshot_id": str(snapshot_id),
                        "second_password": PASSWORD,
                    },
                )
                await http.post(
                    "/universe/activate",
                    data={
                        "csrf_token": CSRF,
                        "snapshot_id": str(snapshot_id),
                        "approval_id": _approval_id(approved.text),
                    },
                )

        async with sessions() as session:
            repository = UniverseAuthorities(session)
            in_august = await repository.membership(
                "KOSPI200", symbol="005930", as_of=date(2026, 9, 10)
            )
            in_october = await repository.membership(
                "KOSPI200", symbol="005930", as_of=date(2026, 10, 1)
            )

        assert in_august.member is True
        assert in_october.member is False

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_page_shows_the_swap_rather_than_only_the_count() -> None:
    """Two lists of two symbols can differ entirely. A member count agrees."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            await http.post("/universe/stage", **_upload(_document()))
            first = (await _snapshot_ids(sessions))[0]
            approved = await http.post(
                "/universe/approve",
                data={
                    "csrf_token": CSRF,
                    "snapshot_id": str(first),
                    "second_password": PASSWORD,
                },
            )
            await http.post(
                "/universe/activate",
                data={
                    "csrf_token": CSRF,
                    "snapshot_id": str(first),
                    "approval_id": _approval_id(approved.text),
                },
            )
            await http.post(
                "/universe/stage",
                **_upload(
                    _document(
                        effective_date=SEPTEMBER,
                        members=[
                            {"symbol": "035420", "common_stock": True, "sector": "C"},
                            {"symbol": "000660", "common_stock": True, "sector": "IT"},
                        ],
                    )
                ),
            )
            page = await http.get("/universe")

        assert "035420" in page.text
        assert "005930" in page.text

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_there_is_no_way_to_change_one_symbol() -> None:
    """Section 11.5 rules out a row editor, and the way it stays ruled out is
    that no route accepts a symbol."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, _ = await _signed_in(sessions, approvals)

        universe_routes = {
            route.path  # type: ignore[attr-defined]
            for route in app.routes
            if getattr(route, "path", "").startswith("/universe")
        }

        assert universe_routes == {
            "/universe",
            "/universe/stage",
            "/universe/approve",
            "/universe/activate",
        }

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_upload_is_audited_even_though_it_changes_nothing() -> None:
    """ "Who uploaded the list that was later activated" is a question the
    activation record alone cannot answer."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            await http.post("/universe/stage", **_upload(_document()))

        async with sessions() as session:
            actions = (
                await session.scalars(
                    select(BackofficeCommandRow.action).where(
                        BackofficeCommandRow.target_type == "UNIVERSE_SNAPSHOT"
                    )
                )
            ).all()

        assert "STAGE_UNIVERSE_SNAPSHOT" in set(actions)

    _drive(scenario)
