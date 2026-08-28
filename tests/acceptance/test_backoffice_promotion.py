"""Section 11.7, against section 17's constraint.

The screen claims sessions and shows progress. It does not decide that a
manifest verified — the repository counts the evidence and refuses. So the
tests that matter here are the ones that try to get readiness out of the screen
without earning it.
"""

from __future__ import annotations

import asyncio
from base64 import b64encode
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid7

import httpx
import pytest
from conftest import integration_database_url, integration_redis_url
from fastapi import FastAPI
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
from autotrader.apps.backoffice.ledger import SOLE_OPERATOR_EMAIL
from autotrader.apps.backoffice.promotion_read_model import PromotionReadModel
from autotrader.apps.backoffice.second_password import (
    APPROVAL_PREFIX,
    ATTEMPT_PREFIX,
    ApprovalStore,
)
from autotrader.config.settings import Settings
from autotrader.execution.promotion.models import PromotionMode, SessionStatus
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.accounts import Account, Broker
from autotrader.persistence.mysql.models.bindings import ProviderAccountBinding
from autotrader.persistence.mysql.models.promotion import PromotionSessionRow
from autotrader.persistence.mysql.repositories.promotion import (
    PromotionRefusedError,
    PromotionSessions,
)
from autotrader.security.secret_crypto import MasterKeyRing

ALLOWED = SOLE_OPERATOR_EMAIL
CSRF = "a-form-token"
BASE_URL = "https://backoffice.example.com"
KEY = b64encode(b"k" * 32).decode()
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
YESTERDAY = date(2026, 8, 26)


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


async def _binding(sessions: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    async with sessions() as session:
        broker = Broker(id=uuid7(), code="KIS", name="KIS")
        existing = await session.scalar(select(Broker).where(Broker.code == "KIS"))
        if existing is None:
            session.add(broker)
            await session.flush()
            broker_id = broker.id
        else:
            broker_id = existing.id
        account = Account(
            id=uuid7(),
            broker_id=broker_id,
            account_alias="promotion-account",
            environment="PAPER",
            secret_reference="secret://none",
            enabled=False,
        )
        session.add(account)
        await session.flush()
        binding = ProviderAccountBinding(
            id=uuid7(),
            account_id=account.id,
            broker_id=broker_id,
            provider_code="KIS",
            environment="PAPER",
            account_seq=None,
            revision=1,
            observed_at=NOW - timedelta(days=7),
            active=True,
        )
        session.add(binding)
        await session.commit()
        return binding.id, account.id


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
def test_the_requirement_is_on_the_page() -> None:
    """Two distinct Shadow and two distinct Paper, said in words rather than
    left for the operator to infer from a counter."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await _binding(sessions)
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            body = (await http.get("/promotion")).text

        assert "Shadow 2회" in body
        assert "Paper 2회" in body

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_session_cannot_be_claimed_without_a_manifest() -> None:
    """A session is a session of some exact strategy build, and there is not
    one to name."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        binding_id, _ = await _binding(sessions)
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            response = await http.post(
                "/promotion/claim",
                data={
                    "csrf_token": CSRF,
                    "binding_id": str(binding_id),
                    "mode": "SHADOW",
                    "exchange_date": YESTERDAY.isoformat(),
                },
            )

        assert response.status_code == 409
        async with sessions() as session:
            rows = (await session.scalars(select(PromotionSessionRow))).all()
            found = len(rows)
            await session.rollback()
        assert found == 0

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_day_with_no_evidence_cannot_be_completed() -> None:
    """The one thing section 17 forbids: turning missing evidence into
    readiness."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        binding_id, account_id = await _binding(sessions)
        async with sessions() as session:
            claimed = await PromotionSessions(session).claim(
                binding_id=binding_id,
                account_id=account_id,
                manifest_id=await _manifest(session),
                mode=PromotionMode.SHADOW,
                exchange_date=YESTERDAY,
                now=NOW,
            )
            await session.commit()

        async with sessions() as session:
            with pytest.raises(PromotionRefusedError, match="NO_DECISIONS"):
                await PromotionSessions(session).complete(
                    session_id=claimed.id, now=NOW, today=NOW.date()
                )
            await session.rollback()

        async with sessions() as session:
            row = await session.scalar(
                select(PromotionSessionRow).where(PromotionSessionRow.id == claimed.id)
            )
            assert row is not None
            status = row.status
            await session.rollback()
        assert status == SessionStatus.CLAIMED.value

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_same_day_cannot_be_claimed_twice() -> None:
    """One day observed twice is not two days."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        binding_id, account_id = await _binding(sessions)
        async with sessions() as session:
            manifest_id = await _manifest(session)
            repository = PromotionSessions(session)
            await repository.claim(
                binding_id=binding_id,
                account_id=account_id,
                manifest_id=manifest_id,
                mode=PromotionMode.SHADOW,
                exchange_date=YESTERDAY,
                now=NOW,
            )
            await session.commit()

        async with sessions() as session:
            with pytest.raises(PromotionRefusedError, match="이미"):
                await PromotionSessions(session).claim(
                    binding_id=binding_id,
                    account_id=account_id,
                    manifest_id=manifest_id,
                    mode=PromotionMode.SHADOW,
                    exchange_date=YESTERDAY,
                    now=NOW,
                )
            await session.rollback()

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_database_refuses_a_complete_row_with_blockers() -> None:
    """Written as a check constraint so a screen, a CLI and a stray SQL client
    are all held to it."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        binding_id, account_id = await _binding(sessions)
        async with sessions() as session:
            manifest_id = await _manifest(session)
            session.add(
                PromotionSessionRow(
                    id=uuid7(),
                    binding_id=binding_id,
                    account_id=account_id,
                    manifest_id=manifest_id,
                    mode="PAPER",
                    exchange_date=YESTERDAY,
                    status="COMPLETE",
                    claimed_at=NOW - timedelta(hours=8),
                    completed_at=NOW,
                    decision_count=10,
                    order_count=2,
                    # An open blocking incident. The row claims COMPLETE anyway.
                    blocking_incident_count=1,
                    blocking_reconciliation_count=0,
                    unresolved_unknown_count=0,
                    evidence_digest=b"d" * 32,
                )
            )
            with pytest.raises(Exception, match="ck_exec_promotion_session_verified"):
                await session.commit()
            await session.rollback()

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_progress_counts_distinct_dates_only() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        binding_id, account_id = await _binding(sessions)
        async with sessions() as session:
            manifest_id = await _manifest(session)
            for day in (25, 24):
                session.add(
                    PromotionSessionRow(
                        id=uuid7(),
                        binding_id=binding_id,
                        account_id=account_id,
                        manifest_id=manifest_id,
                        mode="SHADOW",
                        exchange_date=date(2026, 8, day),
                        status="COMPLETE",
                        claimed_at=NOW - timedelta(days=3),
                        completed_at=NOW - timedelta(days=2),
                        decision_count=5,
                        order_count=0,
                        blocking_incident_count=0,
                        blocking_reconciliation_count=0,
                        unresolved_unknown_count=0,
                        evidence_digest=b"d" * 32,
                    )
                )
            await session.commit()

        view = await PromotionReadModel(sessions).load(today=NOW.date())
        progress = next(item for item in view.bindings if item.binding_id == binding_id)

        assert progress.shadow_remaining == 0
        # Shadow alone is not readiness.
        assert progress.paper_remaining == 2
        assert progress.ready is False

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_page_needs_a_session() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, _ = await _signed_in(sessions, approvals)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            follow_redirects=False,
        ) as http:
            response = await http.get("/promotion")

        assert response.status_code == 303
        assert response.text == ""

    _drive(scenario)


async def _manifest(session: AsyncSession) -> UUID:
    """The registered strategy build, or one seeded for the test."""
    from autotrader.persistence.mysql.models.david_v6 import DavidV6ManifestRow
    from autotrader.persistence.mysql.models.strategy import (
        StrategyDefinition,
        StrategyVersion,
    )

    existing = await session.scalar(select(DavidV6ManifestRow))
    if existing is not None:
        return existing.id
    definition = StrategyDefinition(
        id=uuid7(),
        code="DAVID_TRULLAS_V6",
        research_only=False,
        configuration_hash=b"c" * 32,
    )
    session.add(definition)
    await session.flush()
    version = StrategyVersion(
        id=uuid7(),
        definition_id=definition.id,
        version="v6.0-op-20260824.1",
        # SHADOW is where a version starts; LIVE_APPROVED is what these
        # sessions exist to earn.
        status="SHADOW",
        research_only=False,
    )
    session.add(version)
    await session.flush()
    manifest = DavidV6ManifestRow(
        id=uuid7(),
        strategy_version_id=version.id,
        strategy_code="DAVID_TRULLAS_V6",
        strategy_version="v6.0-op-20260824.1",
        source_sha256=b"s" * 32,
        design_sha256=b"d" * 32,
        configuration_hash=b"c" * 32,
        registered_at=NOW - timedelta(days=10),
    )
    session.add(manifest)
    await session.flush()
    return manifest.id
