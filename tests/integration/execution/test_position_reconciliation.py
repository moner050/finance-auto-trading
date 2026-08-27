"""Position drift against a real MySQL.

Reconciliation only earns its name if a disagreement about what is held
survives the round trip: it has to become a run, a difference naming the
instrument, and an incident an operator can find. The broker side is a fake
here because the comparison is what is under test, not the credentials.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid7

import pytest
from conftest import integration_database_url
from integration.risk.test_concurrent_reservation import _seed as _risk_seed
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.execution.reconciliation.models import (
    BrokerSnapshot,
    HeldPosition,
    ReconciliationDiffKind,
)
from autotrader.execution.reconciliation.service import ReconciliationService
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.accounts import Account
from autotrader.persistence.mysql.models.operations import OpsIncident
from autotrader.persistence.mysql.models.positions import Position
from autotrader.persistence.mysql.models.reconciliation import (
    PersistedReconciliationDiff,
    PersistedReconciliationRun,
)
from autotrader.persistence.mysql.repositories.reconciliation import (
    ReconciliationRepository,
)

NOW = datetime(2026, 8, 27, tzinfo=UTC)


class _Broker:
    """A broker that reports exactly what the scenario tells it to."""

    def __init__(self, *, broker_id: UUID, held: tuple[HeldPosition, ...]) -> None:
        self._broker_id = broker_id
        self._held = held

    async def read_snapshot(self, *, account_id: object) -> BrokerSnapshot:
        assert isinstance(account_id, UUID)
        return BrokerSnapshot(
            broker_id=self._broker_id,
            account_id=account_id,
            complete=True,
            expires_at=NOW + timedelta(minutes=1),
            open_orders=(),
            positions=self._held,
        )


class _Store:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def persist_run(self, run: object) -> object:
        return await ReconciliationRepository(self._session).persist_run(run)  # type: ignore[arg-type]


async def _hold(
    sessions: async_sessionmaker[object],
    *,
    account_id: UUID,
    instrument_id: UUID,
    quantity: Decimal,
) -> None:
    async with sessions() as session:  # type: ignore[operator]
        session.add(
            Position(
                id=uuid7(),
                account_id=account_id,
                instrument_id=instrument_id,
                quantity=quantity,
                average_cost=Decimal("100"),
                currency="USD",
                settlement_asset=None,
                observed_at=NOW,
                blocking_risk=False,
            )
        )
        await session.commit()


async def _broker_id(sessions: async_sessionmaker[object], account_id: UUID) -> UUID:
    async with sessions() as session:  # type: ignore[operator]
        broker_id = await session.scalar(
            select(Account.broker_id).where(Account.id == account_id)
        )
        assert broker_id is not None
        return broker_id


async def _diff_of(
    session: AsyncSession, run: object
) -> PersistedReconciliationDiff | None:
    return await session.scalar(
        select(PersistedReconciliationDiff).where(
            PersistedReconciliationDiff.run_id == run.id  # type: ignore[attr-defined]
        )
    )


def _drive(scenario: object) -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")

    async def run() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await scenario(sessions)  # type: ignore[operator]
        finally:
            await engine.dispose()

    asyncio.run(run())


async def _reconcile(
    sessions: async_sessionmaker[object],
    *,
    account_id: UUID,
    held: tuple[HeldPosition, ...],
) -> object:
    broker_id = await _broker_id(sessions, account_id)
    async with sessions() as session:  # type: ignore[operator]
        repository = ReconciliationRepository(session)
        run = await ReconciliationService().run(
            now=NOW,
            account_id=account_id,
            reader=_Broker(broker_id=broker_id, held=held),
            store=_Store(session),  # type: ignore[arg-type]
            internal_open_orders=await repository.internal_open_orders(
                account_id=account_id
            ),
            internal_positions=await repository.internal_positions(
                account_id=account_id
            ),
        )
        await session.commit()
        return run


@pytest.mark.integration
def test_two_sides_that_agree_report_no_drift() -> None:
    async def scenario(sessions: async_sessionmaker[object]) -> None:
        ids = await _risk_seed(sessions)
        await _hold(
            sessions,
            account_id=ids.account_id,
            instrument_id=ids.instrument_id,
            quantity=Decimal("3"),
        )

        run = await _reconcile(
            sessions,
            account_id=ids.account_id,
            held=(
                HeldPosition(instrument_id=ids.instrument_id, quantity=Decimal("3")),
            ),
        )

        assert run.diffs == ()  # type: ignore[attr-defined]
        async with sessions() as session:  # type: ignore[operator]
            # Scoped to this run: the account seed carries a diff of its own.
            assert await _diff_of(session, run) is None
            stored = await session.get(PersistedReconciliationRun, run.id)  # type: ignore[attr-defined]
            assert stored is not None
            assert stored.status == "SUCCEEDED"

    _drive(scenario)


@pytest.mark.integration
def test_a_quantity_the_broker_disagrees_with_becomes_an_incident() -> None:
    async def scenario(sessions: async_sessionmaker[object]) -> None:
        ids = await _risk_seed(sessions)
        await _hold(
            sessions,
            account_id=ids.account_id,
            instrument_id=ids.instrument_id,
            quantity=Decimal("3"),
        )

        # The broker says two. Every size calculated from three is wrong.
        run = await _reconcile(
            sessions,
            account_id=ids.account_id,
            held=(
                HeldPosition(instrument_id=ids.instrument_id, quantity=Decimal("2")),
            ),
        )

        assert [diff.kind for diff in run.diffs] == [  # type: ignore[attr-defined]
            ReconciliationDiffKind.POSITION_QUANTITY_MISMATCH
        ]
        async with sessions() as session:  # type: ignore[operator]
            diff = await _diff_of(session, run)
            assert diff is not None
            assert diff.severity == "BLOCKING"
            assert diff.status == "OPEN"
            # The report has to name what an operator should go and look at.
            assert diff.instrument_id == ids.instrument_id
            assert diff.internal_order_id is None
            incident = await session.scalar(
                select(OpsIncident).where(
                    OpsIncident.reason_code
                    == "RECONCILIATION_POSITION_QUANTITY_MISMATCH"
                )
            )
            assert incident is not None
            assert incident.scope_type == "INSTRUMENT"
            assert incident.scope_key == str(ids.instrument_id)

    _drive(scenario)


@pytest.mark.integration
def test_a_position_the_broker_never_heard_of_becomes_an_incident() -> None:
    async def scenario(sessions: async_sessionmaker[object]) -> None:
        ids = await _risk_seed(sessions)
        await _hold(
            sessions,
            account_id=ids.account_id,
            instrument_id=ids.instrument_id,
            quantity=Decimal("3"),
        )

        run = await _reconcile(sessions, account_id=ids.account_id, held=())

        assert [diff.kind for diff in run.diffs] == [  # type: ignore[attr-defined]
            ReconciliationDiffKind.INTERNAL_POSITION_BROKER_MISSING
        ]

    _drive(scenario)


@pytest.mark.integration
def test_a_position_only_the_broker_holds_becomes_an_incident() -> None:
    async def scenario(sessions: async_sessionmaker[object]) -> None:
        ids = await _risk_seed(sessions)

        run = await _reconcile(
            sessions,
            account_id=ids.account_id,
            held=(
                HeldPosition(instrument_id=ids.instrument_id, quantity=Decimal("1")),
            ),
        )

        assert [diff.kind for diff in run.diffs] == [  # type: ignore[attr-defined]
            ReconciliationDiffKind.BROKER_POSITION_INTERNAL_MISSING
        ]

    _drive(scenario)


@pytest.mark.integration
def test_a_flat_account_on_both_sides_is_clean() -> None:
    async def scenario(sessions: async_sessionmaker[object]) -> None:
        ids = await _risk_seed(sessions)

        run = await _reconcile(sessions, account_id=ids.account_id, held=())

        # Holding nothing and being told nothing is held is agreement, not a
        # difference in every instrument ever traded.
        assert run.diffs == ()  # type: ignore[attr-defined]

    _drive(scenario)
