"""Section 11.6: what the providers said, shown without quoting them.

The load-bearing claim of this screen is a negative one — no provider payload
and no secret reaches it — so that is what most of these check. The rest check
that an open mismatch and a key that can move funds are impossible to miss.
"""

from __future__ import annotations

import asyncio
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid7

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
from autotrader.apps.backoffice.evidence_read_model import EvidenceReadModel
from autotrader.apps.backoffice.ledger import SOLE_OPERATOR_EMAIL
from autotrader.apps.backoffice.second_password import (
    APPROVAL_PREFIX,
    ATTEMPT_PREFIX,
    ApprovalStore,
)
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.accounts import Account, Broker
from autotrader.persistence.mysql.models.binance_usdm import (
    BinanceUsdmConfigurationFactRow,
    BinanceUsdmReconciliationRunRow,
)
from autotrader.persistence.mysql.models.bindings import ProviderAccountBinding
from autotrader.persistence.mysql.models.reconciliation import (
    PersistedReconciliationDiff,
    PersistedReconciliationRun,
)
from autotrader.security.secret_crypto import MasterKeyRing

ALLOWED = SOLE_OPERATOR_EMAIL
CSRF = "a-form-token"
BASE_URL = "https://backoffice.example.com"
KEY = b64encode(b"k" * 32).decode()
NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)


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


async def _account(sessions: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    async with sessions() as session:
        broker = Broker(id=uuid7(), code=f"B{uuid7().hex[:6]}", name="Test broker")
        session.add(broker)
        await session.flush()
        account = Account(
            id=uuid7(),
            broker_id=broker.id,
            account_alias="evidence-account",
            environment="PAPER",
            secret_reference="secret://none",
            enabled=False,
        )
        session.add(account)
        await session.commit()
        return account.id, broker.id


async def _run_with_diff(
    sessions: async_sessionmaker[AsyncSession],
    *,
    account_id: UUID,
    broker_id: UUID,
    severity: str = "BLOCKING",
) -> None:
    async with sessions() as session:
        run = PersistedReconciliationRun(
            id=uuid7(),
            broker_id=broker_id,
            account_id=account_id,
            started_at=NOW - timedelta(minutes=5),
            completed_at=NOW - timedelta(minutes=5),
            status="SUCCEEDED",
            snapshot_hash=b"s" * 32,
            complete=True,
        )
        session.add(run)
        await session.flush()
        session.add(
            PersistedReconciliationDiff(
                id=uuid7(),
                run_id=run.id,
                # BROKER_OPEN_INTERNAL_MISSING is exactly the case where
                # there is no internal order: the broker is holding one we
                # never placed. The service records None, and so does this.
                internal_order_id=None,
                instrument_id=None,
                broker_order_id="BINANCE-USDM:42",
                broker_execution_id=None,
                diff_key="BROKER_OPEN_INTERNAL_MISSING",
                severity=severity,
                status="OPEN",
                expected_hash=b"e" * 32,
                observed_hash=b"o" * 32,
                created_at=NOW - timedelta(minutes=5),
            )
        )
        await session.commit()


async def _permission(
    sessions: async_sessionmaker[AsyncSession], *, transfer_out: bool
) -> None:
    """A configuration fact hangs off a real run, which hangs off a real
    binding. Inventing the ids would test a row shape the database refuses."""
    async with sessions() as session:
        broker = Broker(id=uuid7(), code="BINANCE", name="Binance")
        session.add(broker)
        await session.flush()
        account = Account(
            id=uuid7(),
            broker_id=broker.id,
            account_alias="binance-evidence",
            environment="LIVE",
            secret_reference="secret://none",
            enabled=False,
        )
        session.add(account)
        await session.flush()
        binding = ProviderAccountBinding(
            id=uuid7(),
            account_id=account.id,
            broker_id=broker.id,
            provider_code="BINANCE",
            environment="LIVE",
            account_seq=None,
            revision=1,
            observed_at=NOW - timedelta(days=1),
            active=True,
        )
        session.add(binding)
        await session.flush()
        run = BinanceUsdmReconciliationRunRow(
            id=uuid7(),
            binding_id=binding.id,
            account_id=account.id,
            provider_code="BINANCE",
            market_code="USD-M",
            symbol="BTCUSDT",
            settlement_asset="USDT",
            provider_as_of=NOW - timedelta(hours=3),
            started_at=NOW - timedelta(hours=2, minutes=1),
            completed_at=NOW - timedelta(hours=2),
            result="COMPLETE",
            balance_fact_count=1,
            position_fact_count=1,
            order_fact_count=0,
            algo_order_fact_count=0,
            trade_fact_count=0,
            income_fact_count=0,
            configuration_fact_count=1,
            fact_digest=b"f" * 32,
            blockers=[],
        )
        session.add(run)
        await session.flush()
        session.add(
            BinanceUsdmConfigurationFactRow(
                id=uuid7(),
                run_id=run.id,
                position_mode="ONE_WAY",
                margin_type="CROSSED",
                auto_add_margin=False,
                leverage=7,
                can_trade=True,
                multi_assets_margin=False,
                transfer_out_enabled=transfer_out,
                maximum_notional=Decimal("100000"),
                price_tick_size=Decimal("0.1"),
                minimum_quantity=Decimal("0.001"),
                quantity_step_size=Decimal("0.001"),
                minimum_notional=Decimal("5"),
                captured_at=NOW - timedelta(hours=2),
            )
        )
        await session.commit()


@pytest.mark.acceptance
@pytest.mark.integration
def test_an_open_mismatch_is_on_the_page() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        account_id, broker_id = await _account(sessions)
        await _run_with_diff(sessions, account_id=account_id, broker_id=broker_id)
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            body = (await http.get("/evidence")).text

        assert "BROKER_OPEN_INTERNAL_MISSING" in body
        assert "BINANCE-USDM:42" in body

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_no_digest_is_shown_in_full() -> None:
    """A digest is for telling two runs apart, not for reading."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        account_id, broker_id = await _account(sessions)
        await _run_with_diff(sessions, account_id=account_id, broker_id=broker_id)
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            body = (await http.get("/evidence")).text

        for digest in (b"s" * 32, b"e" * 32, b"o" * 32):
            assert digest.hex() not in body

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_the_configured_client_secret_never_reaches_the_page() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            body = (await http.get("/evidence")).text

        assert "a-secret-that-must-never-render" not in body

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_key_that_can_move_funds_is_shown_as_such() -> None:
    """A trading key that can withdraw is a different risk from one that
    cannot, and the provider is the only authority on which it is."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await _permission(sessions, transfer_out=True)
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            body = (await http.get("/evidence")).text

        assert "출금 가능" in body
        # Flagged, not merely listed.
        assert 'class="warn"' in body

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_what_cannot_be_shown_says_so() -> None:
    """A blank panel reads as "nothing happened". These are unavailable, which
    is a different fact."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        app, session_id = await _signed_in(sessions, approvals)

        async with _client(app, session_id) as http:
            body = (await http.get("/evidence")).text

        assert "rate limit" in body
        assert "여기서 볼 수 없는 것" in body

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
            response = await http.get("/evidence")

        assert response.status_code == 303
        assert response.text == ""

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_blocking_mismatch_is_counted_not_merely_noticed() -> None:
    """ "Three blocking mismatches" and "one" are different situations."""

    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        account_id, broker_id = await _account(sessions)
        await _run_with_diff(sessions, account_id=account_id, broker_id=broker_id)
        view = await EvidenceReadModel(sessions).load(now=NOW)

        run = next(item for item in view.runs if item.source == "LOOP")
        assert run.blocking_diffs == 1
        assert run.clean is False

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_freshness_is_stated_as_elapsed_time() -> None:
    async def scenario(
        sessions: async_sessionmaker[AsyncSession], approvals: object
    ) -> None:
        await _permission(sessions, transfer_out=False)
        view = await EvidenceReadModel(sessions).load(now=NOW)

        assert view.permissions[0].age == "2시간"

    _drive(scenario)
