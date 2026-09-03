"""The risk policy screen.

The only question worth asking of this screen is whether activating a version
changes how a trade is sized. Everything else it shows is in service of an
operator being able to answer that before they press the button.
"""

from __future__ import annotations

import asyncio
from base64 import b64encode
from datetime import UTC, datetime
from decimal import Decimal
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
from autotrader.apps.backoffice.policies_read_model import (
    PoliciesReadModel,
    as_percentage,
    difference,
)
from autotrader.apps.backoffice.policy_commands import PolicyCommandRefusedError
from autotrader.apps.backoffice.second_password import (
    APPROVAL_PREFIX,
    ATTEMPT_PREFIX,
    ApprovalStore,
    MySqlSecondPasswords,
)
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.risk import RiskPolicy, RiskPolicyVersion
from autotrader.persistence.mysql.repositories.david_v6_risk import (
    APPROVED_V6_RISK_POLICIES,
    V6_RISK_POLICY_VERSION,
    DavidV6RiskRepository,
    policy_row_refusal,
)
from autotrader.risk.v6 import MAX_LEVERAGE, SESSION_TRADE_UPPER_BOUND
from autotrader.security.secret_crypto import MasterKeyRing
from autotrader.strategies.david_v6.models import V6Market

ALLOWED = "operator@example.com"
CSRF = "a-form-token"
BASE_URL = "https://backoffice.example.com"
KEY = b64encode(b"k" * 32).decode()
PASSWORD = "correct horse battery staple"
NOW = datetime(2026, 8, 27, tzinfo=UTC)

_KRW = next(
    item for item in APPROVED_V6_RISK_POLICIES if item.code == "DAVID_V6_CASH_KRW"
)
# The row the loop will accept: the approved authority, field for field. A row
# that differs is not a different policy, it is a policy the loop refuses.
_APPROVED: dict[str, object] = {
    "normal_risk_fraction": _KRW.normal_risk_fraction,
    "a_candidate_risk_fraction": _KRW.a_candidate_risk_fraction,
    "a_risk_fraction": _KRW.a_risk_fraction,
    "absolute_trade_risk_fraction": _KRW.absolute_trade_risk_fraction,
    "daily_loss_fraction": _KRW.daily_loss_fraction,
    "weekly_loss_fraction": _KRW.weekly_loss_fraction,
    "max_consecutive_losses": _KRW.max_consecutive_losses,
    "max_open_structural_risk_fraction": _KRW.max_open_structural_risk_fraction,
    "account_age_seconds": _KRW.account_age_seconds,
    "risk_age_seconds": _KRW.risk_age_seconds,
    "quote_age_seconds": _KRW.quote_age_seconds,
    "provider_age_seconds": _KRW.provider_age_seconds,
    "stream_gap_age_seconds": _KRW.stream_gap_age_seconds,
    "completed_intraday_bar_arrival_seconds": (
        _KRW.completed_intraday_bar_arrival_seconds
    ),
    "daily_requires_authoritative_close": _KRW.daily_requires_authoritative_close,
}
APPROVED_VERSION = V6_RISK_POLICY_VERSION


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
            await scenario(sessions, client)  # type: ignore[operator]
        finally:
            for prefix in (APPROVAL_PREFIX, ATTEMPT_PREFIX):
                keys = [key async for key in client.scan_iter(f"{prefix}*")]
                if keys:
                    await client.delete(*keys)
            await client.aclose()
            await engine.dispose()

    asyncio.run(run())


async def _seed_policy(
    sessions: async_sessionmaker[AsyncSession],
    *,
    versions: tuple[tuple[str, bool, dict[str, object]], ...],
    code: str = "DAVID_V6_CASH_KRW",
) -> dict[str, UUID]:
    """One policy with the given versions, the flagged one active.

    The code is the approved one because that is the only code the loop will
    load, and a screen tested against a code the loop rejects tests nothing.
    """
    identifiers: dict[str, UUID] = {}
    async with sessions() as session:
        existing = await session.scalar(
            select(RiskPolicy).where(RiskPolicy.code == code)
        )
        if existing is not None:
            await session.execute(
                delete(RiskPolicyVersion).where(
                    RiskPolicyVersion.policy_id == existing.id
                )
            )
            await session.delete(existing)
            await session.flush()
        policy = RiskPolicy(id=uuid7(), code=code, active=True)
        session.add(policy)
        await session.flush()
        for name, active, changes in versions:
            values = dict(_APPROVED)
            values.update(changes)
            row = RiskPolicyVersion(
                id=uuid7(), policy_id=policy.id, version=name, active=active, **values
            )
            session.add(row)
            identifiers[name] = row.id
        await session.commit()
    return identifiers


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


def _approval_id(body: str) -> str:
    marker = 'name="approval_id" value="'
    start = body.index(marker) + len(marker)
    return body[start : body.index('"', start)]


async def _in_force(
    sessions: async_sessionmaker[AsyncSession], ids: dict[str, UUID]
) -> set[str]:
    async with sessions() as session:
        rows = (
            await session.scalars(
                select(RiskPolicyVersion).where(RiskPolicyVersion.id.in_(ids.values()))
            )
        ).all()
    return {row.version for row in rows if row.active}


async def _clear_policy(sessions: async_sessionmaker[AsyncSession], code: str) -> None:
    """Leave the policy absent, so creation has something to create."""
    async with sessions() as session:
        existing = await session.scalar(
            select(RiskPolicy).where(RiskPolicy.code == code)
        )
        if existing is not None:
            await session.execute(
                delete(RiskPolicyVersion).where(
                    RiskPolicyVersion.policy_id == existing.id
                )
            )
            await session.delete(existing)
        await session.commit()


STALE = "v6-op-20260101.1"


@pytest.mark.acceptance
@pytest.mark.integration
def test_activating_the_approved_version_puts_it_in_force() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        ids = await _seed_policy(
            sessions,
            versions=(
                (STALE, True, {"normal_risk_fraction": Decimal("0.0005")}),
                (APPROVED_VERSION, False, {}),
            ),
        )
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            approved = await http.post(
                "/policies/approve",
                data={
                    "csrf_token": CSRF,
                    "target_version_id": str(ids[APPROVED_VERSION]),
                    "second_password": PASSWORD,
                },
            )
            await http.post(
                "/policies/apply",
                data={
                    "csrf_token": CSRF,
                    "target_version_id": str(ids[APPROVED_VERSION]),
                    "approval_id": _approval_id(approved.text),
                },
            )

        # Exactly one. Between the two the market would have no policy, and
        # the engine refuses to size without one.
        assert await _in_force(sessions, ids) == {APPROVED_VERSION}

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_loop_then_loads_the_version_the_screen_armed() -> None:
    """The only question worth asking of this screen."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        ids = await _seed_policy(
            sessions,
            versions=(
                (STALE, True, {"normal_risk_fraction": Decimal("0.0005")}),
                (APPROVED_VERSION, False, {}),
            ),
        )
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            approved = await http.post(
                "/policies/approve",
                data={
                    "csrf_token": CSRF,
                    "target_version_id": str(ids[APPROVED_VERSION]),
                    "second_password": PASSWORD,
                },
            )
            await http.post(
                "/policies/apply",
                data={
                    "csrf_token": CSRF,
                    "target_version_id": str(ids[APPROVED_VERSION]),
                    "approval_id": _approval_id(approved.text),
                },
            )

        async with sessions() as session:
            snapshot = await DavidV6RiskRepository(session).load_active_policy(
                code="DAVID_V6_CASH_KRW", market=V6Market.KRX_CASH
            )
            await session.rollback()

        assert snapshot is not None
        assert snapshot.policy_version_id == ids[APPROVED_VERSION]

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_version_the_loop_would_refuse_is_refused_at_the_desk() -> None:
    """Otherwise the refusal arrives as a trader that will not start."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        unapproved = "v6-op-20261231.9"
        ids = await _seed_policy(
            sessions,
            versions=(
                (APPROVED_VERSION, True, {}),
                # Within every bound the schema checks, and still not the
                # numbers the loop was approved to trade.
                (unapproved, False, {"normal_risk_fraction": Decimal("0.0010")}),
            ),
        )
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            with pytest.raises(PolicyCommandRefusedError):
                await http.post(
                    "/policies/approve",
                    data={
                        "csrf_token": CSRF,
                        "target_version_id": str(ids[unapproved]),
                    },
                )

        assert await _in_force(sessions, ids) == {APPROVED_VERSION}

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_panel_shows_the_fields_that_move() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        ids = await _seed_policy(
            sessions,
            versions=(
                (
                    STALE,
                    True,
                    {
                        "normal_risk_fraction": Decimal("0.0005"),
                        "max_consecutive_losses": 1,
                    },
                ),
                (APPROVED_VERSION, False, {}),
            ),
        )
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            panel = await http.post(
                "/policies/approve",
                data={
                    "csrf_token": CSRF,
                    "target_version_id": str(ids[APPROVED_VERSION]),
                },
            )

        body = panel.text
        # Confirming a change you cannot see is confirming a version number.
        assert "normal_risk_fraction" in body
        assert "max_consecutive_losses" in body
        # Both the stored fraction and its reading.
        assert "0.0005" in body
        assert as_percentage(Decimal("0.0005")) in body

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_wrong_password_leaves_the_policy_alone() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        ids = await _seed_policy(
            sessions,
            versions=((STALE, True, {}), (APPROVED_VERSION, False, {})),
        )
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            response = await http.post(
                "/policies/approve",
                data={
                    "csrf_token": CSRF,
                    "target_version_id": str(ids[APPROVED_VERSION]),
                    "second_password": "not the password",
                },
            )

        assert "approval_id" not in response.text
        assert await _in_force(sessions, ids) == {STALE}

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_ceilings_a_policy_cannot_widen_are_on_the_page() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            body = " ".join((await http.get("/policies")).text.split())

        # So nobody goes looking for a field that could raise them. Whitespace
        # is collapsed first: where the template wraps a line is not a fact
        # about the page.
        for label in ("최대 레버리지", "세션 최대 거래", "1거래 최대 리스크"):
            assert label in body
        assert "0.01 (1%)" in body
        assert f"{MAX_LEVERAGE}" in body
        assert f"{SESSION_TRADE_UPPER_BOUND}회" in body
        # The approved capital, which is what an operator checks an account
        # balance against.
        for amount, unit in (("1,000,000", "KRW"), ("2,000", "USD"), ("2,000", "USDT")):
            assert f"{amount} {unit}" in body

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_version_already_in_force_is_refused() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        ids = await _seed_policy(sessions, versions=((APPROVED_VERSION, True, {}),))
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            with pytest.raises(PolicyCommandRefusedError, match="already in force"):
                await http.post(
                    "/policies/approve",
                    data={
                        "csrf_token": CSRF,
                        "target_version_id": str(ids[APPROVED_VERSION]),
                    },
                )

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_an_unknown_version_is_refused_rather_than_guessed() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            response = await http.post(
                "/policies/approve",
                data={"csrf_token": CSRF, "target_version_id": "not-a-uuid"},
            )

        assert response.status_code == 400

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_page_needs_a_session_like_every_other_one() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, _ = await _signed_in(sessions, approvals)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            follow_redirects=False,
        ) as http:
            response = await http.get("/policies")

        assert response.status_code == 303
        assert response.text == ""

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_fraction_present_in_one_version_and_absent_in_the_other_shows() -> None:
    """A cash policy has no A-candidate fraction and a futures one does, which
    is the sort of difference an operator most needs to see."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await _seed_policy(
            sessions,
            versions=(
                (STALE, True, {}),
                (
                    APPROVED_VERSION,
                    False,
                    {"a_candidate_risk_fraction": Decimal("0.0010")},
                ),
            ),
        )
        versions = await PoliciesReadModel(sessions).versions()
        left = next(item for item in versions if item.version == STALE)
        right = next(item for item in versions if item.version == APPROVED_VERSION)

        names = {item.name for item in difference(left, right)}

        assert "a_candidate_risk_fraction" in names

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_created_version_is_one_the_loop_can_load() -> None:
    """Creation is a materialisation of the approved definition, so the row it
    writes cannot be one the loop refuses."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await _clear_policy(sessions, "DAVID_V6_CASH_KRW")
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            await http.post(
                "/policies/create",
                data={"csrf_token": CSRF, "policy_code": "DAVID_V6_CASH_KRW"},
            )

        async with sessions() as session:
            row = await session.scalar(
                select(RiskPolicyVersion)
                .join(RiskPolicy, RiskPolicy.id == RiskPolicyVersion.policy_id)
                .where(RiskPolicy.code == "DAVID_V6_CASH_KRW")
            )
            assert row is not None
            # Inert until activated, and loadable the moment it is.
            assert row.active is False
            assert policy_row_refusal(row, code="DAVID_V6_CASH_KRW") is None
            await session.rollback()

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_definition_already_stored_is_not_offered_again() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await _clear_policy(sessions, "DAVID_V6_CASH_KRW")
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            before = (await http.get("/policies")).text
            await http.post(
                "/policies/create",
                data={"csrf_token": CSRF, "policy_code": "DAVID_V6_CASH_KRW"},
            )
            after = (await http.get("/policies")).text

        offered = 'value="DAVID_V6_CASH_KRW"'
        assert offered in before
        assert offered not in after

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_creating_the_same_version_twice_is_refused() -> None:
    """The version is the identity. A second row under it would make "which one
    is in force" a question with two answers."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await _clear_policy(sessions, "DAVID_V6_CASH_KRW")
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            await http.post(
                "/policies/create",
                data={"csrf_token": CSRF, "policy_code": "DAVID_V6_CASH_KRW"},
            )
            with pytest.raises(PolicyCommandRefusedError, match="already stored"):
                await http.post(
                    "/policies/create",
                    data={"csrf_token": CSRF, "policy_code": "DAVID_V6_CASH_KRW"},
                )

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_an_unapproved_code_creates_nothing() -> None:
    """There is no field to type a policy into, and no code to invent one under."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            with pytest.raises(PolicyCommandRefusedError, match="not approved"):
                await http.post(
                    "/policies/create",
                    data={"csrf_token": CSRF, "policy_code": "DAVID_V6_MADE_UP"},
                )

        async with sessions() as session:
            found = await session.scalar(
                select(RiskPolicy).where(RiskPolicy.code == "DAVID_V6_MADE_UP")
            )
            await session.rollback()
        assert found is None

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_created_version_can_then_be_activated_and_traded_under() -> None:
    """Create, activate, and the loop loads it. The three steps the plan
    documents, driven end to end through the screen."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        await _clear_policy(sessions, "DAVID_V6_CASH_KRW")
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            await http.post(
                "/policies/create",
                data={"csrf_token": CSRF, "policy_code": "DAVID_V6_CASH_KRW"},
            )
            async with sessions() as session:
                created = await session.scalar(
                    select(RiskPolicyVersion)
                    .join(RiskPolicy, RiskPolicy.id == RiskPolicyVersion.policy_id)
                    .where(RiskPolicy.code == "DAVID_V6_CASH_KRW")
                )
                assert created is not None
                version_id = created.id
                await session.rollback()

            approved = await http.post(
                "/policies/approve",
                data={
                    "csrf_token": CSRF,
                    "target_version_id": str(version_id),
                    "second_password": PASSWORD,
                },
            )
            await http.post(
                "/policies/apply",
                data={
                    "csrf_token": CSRF,
                    "target_version_id": str(version_id),
                    "approval_id": _approval_id(approved.text),
                },
            )

        async with sessions() as session:
            snapshot = await DavidV6RiskRepository(session).load_active_policy(
                code="DAVID_V6_CASH_KRW", market=V6Market.KRX_CASH
            )
            await session.rollback()

        assert snapshot is not None
        assert snapshot.policy_version_id == version_id

    _drive(scenario)
