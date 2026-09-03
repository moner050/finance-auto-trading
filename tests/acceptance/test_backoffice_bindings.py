"""Binding an account to a risk policy version.

Section 11.4's fourth operation. The question that matters is the same one the
version screen answers: does pressing the button change what the loop does. So
the central test binds through the form and then asks the loop's own resolver
which policy the account trades under.
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
from autotrader.apps.backoffice.second_password import (
    APPROVAL_PREFIX,
    ATTEMPT_PREFIX,
    ApprovalStore,
    MySqlSecondPasswords,
)
from autotrader.apps.trader.composition import UnboundAccountError, bound_policy
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.accounts import Account, Broker
from autotrader.persistence.mysql.models.risk import (
    AccountRiskPolicyBinding,
    RiskPolicy,
    RiskPolicyVersion,
)
from autotrader.persistence.mysql.repositories.david_v6_risk import (
    APPROVED_V6_RISK_POLICIES,
)
from autotrader.persistence.mysql.repositories.policy_binding import (
    ACTIVE,
    AccountPolicyBindings,
    BindingRefusedError,
)
from autotrader.security.secret_crypto import MasterKeyRing
from autotrader.strategies.david_v6.models import V6Market

ALLOWED = "operator@example.com"
CSRF = "a-form-token"
BASE_URL = "https://backoffice.example.com"
KEY = b64encode(b"k" * 32).decode()
PASSWORD = "correct horse battery staple"
NOW = datetime(2026, 8, 27, tzinfo=UTC)

KRW = next(
    item for item in APPROVED_V6_RISK_POLICIES if item.code == "DAVID_V6_CASH_KRW"
)
USDT = next(
    item
    for item in APPROVED_V6_RISK_POLICIES
    if item.code == "DAVID_V6_BINANCE_USDM_USDT"
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
            for prefix in (APPROVAL_PREFIX, ATTEMPT_PREFIX):
                keys = [key async for key in client.scan_iter(f"{prefix}*")]
                if keys:
                    await client.delete(*keys)
            await client.aclose()
            await engine.dispose()

    asyncio.run(run())


def _fields(definition: object) -> dict[str, object]:
    names = (
        "normal_risk_fraction",
        "a_candidate_risk_fraction",
        "a_risk_fraction",
        "absolute_trade_risk_fraction",
        "daily_loss_fraction",
        "weekly_loss_fraction",
        "max_consecutive_losses",
        "max_open_structural_risk_fraction",
        "account_age_seconds",
        "risk_age_seconds",
        "quote_age_seconds",
        "provider_age_seconds",
        "stream_gap_age_seconds",
        "completed_intraday_bar_arrival_seconds",
        "daily_requires_authoritative_close",
    )
    return {name: getattr(definition, name) for name in names}


async def _seed(
    sessions: async_sessionmaker[AsyncSession],
    *,
    definitions: tuple[object, ...] = (KRW,),
    extra_version: tuple[str, dict[str, object]] | None = None,
    active: bool = True,
) -> tuple[UUID, dict[str, UUID]]:
    """One account and the approved version of each named policy."""
    versions: dict[str, UUID] = {}
    async with sessions() as session:
        for definition in definitions:
            code = definition.code  # type: ignore[attr-defined]
            existing = await session.scalar(
                select(RiskPolicy).where(RiskPolicy.code == code)
            )
            if existing is not None:
                await session.execute(
                    delete(AccountRiskPolicyBinding).where(
                        AccountRiskPolicyBinding.policy_version_id.in_(
                            select(RiskPolicyVersion.id).where(
                                RiskPolicyVersion.policy_id == existing.id
                            )
                        )
                    )
                )
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
            row = RiskPolicyVersion(
                id=uuid7(),
                policy_id=policy.id,
                version=definition.version,  # type: ignore[attr-defined]
                active=active,
                **_fields(definition),
            )
            session.add(row)
            versions[code] = row.id
            if extra_version is not None and definition is definitions[0]:
                name, changes = extra_version
                values = dict(_fields(definition))
                values.update(changes)
                spare = RiskPolicyVersion(
                    id=uuid7(),
                    policy_id=policy.id,
                    version=name,
                    active=False,
                    **values,
                )
                session.add(spare)
                versions[name] = spare.id

        broker = Broker(id=uuid7(), code=f"B{uuid7().hex[:6]}", name="Test broker")
        session.add(broker)
        await session.flush()
        account = Account(
            id=uuid7(),
            broker_id=broker.id,
            account_alias=f"acct-{uuid7().hex[:6]}",
            environment="PAPER",
            secret_reference="none",
            enabled=True,
        )
        session.add(account)
        await session.commit()
        return account.id, versions


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


async def _bind(
    app: FastAPI, session_id: str, *, account_id: UUID, version_id: UUID
) -> None:
    async with _client(app, session_id) as http:
        approved = await http.post(
            "/bindings/approve",
            data={
                "csrf_token": CSRF,
                "account_id": str(account_id),
                "target_version_id": str(version_id),
                "second_password": PASSWORD,
            },
        )
        await http.post(
            "/bindings/apply",
            data={
                "csrf_token": CSRF,
                "account_id": str(account_id),
                "target_version_id": str(version_id),
                "approval_id": _approval_id(approved.text),
            },
        )


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_loop_reads_the_policy_the_screen_bound() -> None:
    """The only question worth asking of this screen."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        account_id, versions = await _seed(sessions)
        app, session_id = await _signed_in(sessions, approvals)

        with pytest.raises(UnboundAccountError):
            # Before the binding there is nothing to trade under.
            await bound_policy(
                sessions, account_id=account_id, market=V6Market.KRX_CASH
            )

        await _bind(
            app, session_id, account_id=account_id, version_id=versions[KRW.code]
        )

        bound = await bound_policy(
            sessions, account_id=account_id, market=V6Market.KRX_CASH
        )
        assert bound.policy_version_id == versions[KRW.code]
        assert bound.snapshot.normal_risk_fraction == KRW.normal_risk_fraction

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_rebinding_replaces_rather_than_adds() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        account_id, versions = await _seed(sessions, definitions=(KRW, USDT))
        app, session_id = await _signed_in(sessions, approvals)

        await _bind(
            app, session_id, account_id=account_id, version_id=versions[KRW.code]
        )
        await _bind(
            app, session_id, account_id=account_id, version_id=versions[USDT.code]
        )

        async with sessions() as session:
            rows = (
                await session.scalars(
                    select(AccountRiskPolicyBinding).where(
                        AccountRiskPolicyBinding.account_id == account_id
                    )
                )
            ).all()

        assert len(rows) == 2
        live = [row for row in rows if row.active_marker == ACTIVE]
        assert len(live) == 1
        assert live[0].policy_version_id == versions[USDT.code]
        # The replaced one keeps its place in the chain rather than vanishing.
        retired = next(row for row in rows if row.active_marker is None)
        assert retired.deactivated_at is not None
        assert live[0].previous_binding_id == retired.id

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_scope_comes_from_the_policy_not_from_the_form() -> None:
    """Section 11.4: the GUI does not broaden a market scope."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        account_id, versions = await _seed(sessions, definitions=(USDT,))
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            approved = await http.post(
                "/bindings/approve",
                data={
                    "csrf_token": CSRF,
                    "account_id": str(account_id),
                    "target_version_id": str(versions[USDT.code]),
                    "second_password": PASSWORD,
                },
            )
            await http.post(
                "/bindings/apply",
                data={
                    "csrf_token": CSRF,
                    "account_id": str(account_id),
                    "target_version_id": str(versions[USDT.code]),
                    "approval_id": _approval_id(approved.text),
                    # A field the form does not define. It must not be read.
                    "currency": "KRW",
                    "settlement_asset": "KRW",
                },
            )

        async with sessions() as session:
            row = await session.scalar(
                select(AccountRiskPolicyBinding).where(
                    AccountRiskPolicyBinding.account_id == account_id
                )
            )

        assert row is not None
        assert row.currency is None
        assert row.settlement_asset == "USDT"

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_wrong_password_binds_nothing() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        account_id, versions = await _seed(sessions)
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            response = await http.post(
                "/bindings/approve",
                data={
                    "csrf_token": CSRF,
                    "account_id": str(account_id),
                    "target_version_id": str(versions[KRW.code]),
                    "second_password": "not the password",
                },
            )

        assert "approval_id" not in response.text
        async with sessions() as session:
            binding = await AccountPolicyBindings(session).active_binding(account_id)
        assert binding is None

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_version_the_loop_would_not_load_cannot_be_bound() -> None:
    """Only one version string per policy is the approved one. A row under any
    other is never read by the loop, so binding to it would produce an account
    that cannot trade."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        account_id, versions = await _seed(
            sessions,
            extra_version=(
                "v6-op-20260101.1",
                {"normal_risk_fraction": Decimal("0.0005")},
            ),
        )
        app, session_id = await _signed_in(sessions, approvals)

        with pytest.raises(BindingRefusedError, match="로드되지"):
            await _bind(
                app,
                session_id,
                account_id=account_id,
                version_id=versions["v6-op-20260101.1"],
            )

        async with sessions() as session:
            binding = await AccountPolicyBindings(session).active_binding(account_id)
        assert binding is None

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_version_not_in_force_cannot_be_bound() -> None:
    """Otherwise the account would hold a binding the loop refuses to read."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        account_id, versions = await _seed(sessions, active=False)
        app, session_id = await _signed_in(sessions, approvals)

        with pytest.raises(BindingRefusedError, match="적용 중인 버전만"):
            await _bind(
                app,
                session_id,
                account_id=account_id,
                version_id=versions[KRW.code],
            )

        async with sessions() as session:
            binding = await AccountPolicyBindings(session).active_binding(account_id)
        assert binding is None

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_an_unbound_account_is_shown_as_unable_to_trade() -> None:
    """A list of only bound accounts would hide the one that cannot trade."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        account_id, _ = await _seed(sessions)
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            body = (await http.get("/policies")).text

        assert str(account_id) in body
        assert "매매 불가" in body

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_binding_the_same_version_twice_is_refused() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        account_id, versions = await _seed(sessions)
        app, session_id = await _signed_in(sessions, approvals)

        await _bind(
            app, session_id, account_id=account_id, version_id=versions[KRW.code]
        )

        with pytest.raises(BindingRefusedError, match="이미 이 버전"):
            await _bind(
                app, session_id, account_id=account_id, version_id=versions[KRW.code]
            )

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_binding_for_another_market_does_not_resolve() -> None:
    """Resolution is fail-closed: a Binance policy is not a KRX policy."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        account_id, versions = await _seed(sessions, definitions=(USDT,))
        app, session_id = await _signed_in(sessions, approvals)

        await _bind(
            app, session_id, account_id=account_id, version_id=versions[USDT.code]
        )

        with pytest.raises(UnboundAccountError):
            await bound_policy(
                sessions, account_id=account_id, market=V6Market.KRX_CASH
            )
        bound = await bound_policy(
            sessions, account_id=account_id, market=V6Market.BINANCE_USDM
        )
        assert bound.policy_version_id == versions[USDT.code]

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_pressing_bind_asks_for_the_password_without_a_second_button() -> None:
    """Section 9 keeps the second password on this step. What it does not ask
    for is that reaching it takes three presses.

    Pressing 연결 used to re-render the page with a confirmation section
    somewhere below, where an 승인… button opened the dialog. Both POSTs now
    return the step already open: the password first, then the apply.
    """

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await MySqlSecondPasswords(sessions).establish(PASSWORD, now=NOW)
        account_id, versions = await _seed(sessions)
        version_id = versions[KRW.code]
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            # What the 연결 button in the table posts: no password yet.
            proposed = await http.post(
                "/bindings/approve",
                data={
                    "csrf_token": CSRF,
                    "account_id": str(account_id),
                    "target_version_id": str(version_id),
                },
            )
            approved = await http.post(
                "/bindings/approve",
                data={
                    "csrf_token": CSRF,
                    "account_id": str(account_id),
                    "target_version_id": str(version_id),
                    "second_password": PASSWORD,
                },
            )

        assert proposed.status_code == 200
        assert 'id="confirm-4" data-open' in proposed.text
        # The password is inside the dialog that opens, and so is the change
        # it is approving - the backdrop hides whatever is behind it.
        opened = proposed.text[proposed.text.index('id="confirm-4"') :]
        opened = opened[: opened.index("</dialog>")]
        assert 'name="second_password"' in opened
        assert str(version_id) in opened

        assert approved.status_code == 200
        assert 'id="confirm-4" data-open' in approved.text
        applying = approved.text[approved.text.index('id="confirm-4"') :]
        applying = applying[: applying.index("</dialog>")]
        assert 'action="/bindings/apply"' in applying
        assert 'name="approval_id"' in applying
        # The password is not asked for twice.
        assert 'name="second_password"' not in applying

    _drive(scenario)
