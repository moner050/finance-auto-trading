from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.reconciliation.adoption import BrokerOpenOrderAdoptionService
from autotrader.execution.reconciliation.models import (
    BrokerOpenOrder,
    BrokerOpenOrderAdoption,
    BrokerOpenOrderAdoptionResult,
    BrokerSnapshot,
    InternalOpenOrder,
    ReconciliationDiffKind,
    ReconciliationRun,
)
from autotrader.execution.reconciliation.service import ReconciliationService


def test_reconciliation_reports_missing_orders_and_never_treats_partial_as_clean() -> (
    None
):
    now = datetime.now(UTC)
    internal = InternalOpenOrder(
        order_id=uuid4(),
        broker_order_id="broker-internal",
        broker_client_order_id="client-internal",
    )
    broker_only = BrokerOpenOrder(
        broker_order_id="broker-only",
        broker_client_order_id="client-only",
        canonical_terms_hash=b"b" * 32,
    )
    snapshot = BrokerSnapshot(
        broker_id=uuid4(),
        account_id=uuid4(),
        complete=False,
        expires_at=now + timedelta(minutes=1),
        open_orders=(broker_only,),
    )

    diffs = ReconciliationService().compare(
        now=now, snapshot=snapshot, internal_open_orders=(internal,)
    )

    assert {diff.kind for diff in diffs} == {
        ReconciliationDiffKind.SNAPSHOT_INCOMPLETE,
        ReconciliationDiffKind.INTERNAL_OPEN_BROKER_MISSING,
        ReconciliationDiffKind.BROKER_OPEN_INTERNAL_MISSING,
    }
    assert all(diff.blocking for diff in diffs)


def test_reconciliation_accepts_exact_broker_and_client_order_identity() -> None:
    now = datetime.now(UTC)
    internal = InternalOpenOrder(
        order_id=uuid4(), broker_order_id="broker-1", broker_client_order_id="client-1"
    )
    snapshot = BrokerSnapshot(
        broker_id=uuid4(),
        account_id=uuid4(),
        complete=True,
        expires_at=now + timedelta(minutes=1),
        open_orders=(
            BrokerOpenOrder(
                broker_order_id="broker-1",
                broker_client_order_id="client-1",
                canonical_terms_hash=b"a" * 32,
            ),
        ),
    )

    assert (
        ReconciliationService().compare(
            now=now, snapshot=snapshot, internal_open_orders=(internal,)
        )
        == ()
    )


def test_reconciliation_requires_a_fresh_snapshot() -> None:
    now = datetime.now(UTC)
    snapshot = BrokerSnapshot(
        broker_id=uuid4(),
        account_id=uuid4(),
        complete=True,
        expires_at=now,
        open_orders=(),
    )

    diffs = ReconciliationService().compare(
        now=now, snapshot=snapshot, internal_open_orders=()
    )

    assert [diff.kind for diff in diffs] == [ReconciliationDiffKind.SNAPSHOT_STALE]


def test_adoption_requires_broker_evidence_and_conservative_risk() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="reserved risk"):
        BrokerOpenOrderAdoption(
            reconciliation_diff_id=uuid4(),
            account_id=uuid4(),
            broker_id=uuid4(),
            broker_order_id="broker-1",
            broker_client_order_id="client-1",
            instrument_id=uuid4(),
            side=Side.BUY,
            order_style=OrderStyle.LIMIT,
            requested_quantity=Decimal("1"),
            limit_price=Decimal("10"),
            currency="USD",
            reserved_risk_amount=Decimal("0"),
            policy_version_id=uuid4(),
            risk_snapshot_id=uuid4(),
            observed_at=now,
            reservation_expires_at=now + timedelta(minutes=1),
            payload_hash=b"a" * 32,
        )


@pytest.mark.asyncio
async def test_adoption_service_delegates_without_a_broker_submit() -> None:
    now = datetime.now(UTC)
    adoption = BrokerOpenOrderAdoption(
        reconciliation_diff_id=uuid4(),
        account_id=uuid4(),
        broker_id=uuid4(),
        broker_order_id="broker-1",
        broker_client_order_id="client-1",
        instrument_id=uuid4(),
        side=Side.BUY,
        order_style=OrderStyle.LIMIT,
        requested_quantity=Decimal("1"),
        limit_price=Decimal("10"),
        currency="USD",
        reserved_risk_amount=Decimal("10"),
        policy_version_id=uuid4(),
        risk_snapshot_id=uuid4(),
        observed_at=now,
        reservation_expires_at=now + timedelta(minutes=1),
        payload_hash=b"a" * 32,
    )
    store = _AdoptionStore()

    result = await BrokerOpenOrderAdoptionService(store=store).adopt_open_order(
        adoption
    )

    assert store.calls == [adoption]
    assert result.created


@pytest.mark.asyncio
async def test_reconciliation_run_persists_incomplete_snapshot_as_unsuccessful() -> (
    None
):
    now = datetime.now(UTC)
    account_id = uuid4()
    reader = _SnapshotReader(
        BrokerSnapshot(uuid4(), account_id, False, now + timedelta(minutes=1), ())
    )
    store = _RunStore()

    run = await ReconciliationService().run(
        now=now,
        account_id=account_id,
        reader=reader,
        store=store,
        internal_open_orders=(),
    )

    assert not run.complete and not run.succeeded
    assert store.runs == [run]
    assert run.diffs[0].kind is ReconciliationDiffKind.SNAPSHOT_INCOMPLETE


class _AdoptionStore:
    def __init__(self) -> None:
        self.calls: list[BrokerOpenOrderAdoption] = []

    async def adopt_open_order(
        self, adoption: BrokerOpenOrderAdoption
    ) -> BrokerOpenOrderAdoptionResult:
        self.calls.append(adoption)
        return BrokerOpenOrderAdoptionResult(
            order_id=uuid4(),
            broker_order_id=adoption.broker_order_id,
            reservation_id=uuid4(),
            created=True,
        )


class _SnapshotReader:
    def __init__(self, snapshot: BrokerSnapshot) -> None:
        self._snapshot = snapshot

    async def read_snapshot(self, *, account_id: object) -> BrokerSnapshot:
        return self._snapshot


class _RunStore:
    def __init__(self) -> None:
        self.runs: list[ReconciliationRun] = []

    async def persist_run(self, run: ReconciliationRun) -> ReconciliationRun:
        self.runs.append(run)
        return run
