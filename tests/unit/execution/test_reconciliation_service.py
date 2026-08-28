from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.reconciliation.adoption import BrokerOpenOrderAdoptionService
from autotrader.execution.reconciliation.models import (
    BrokerOpenOrder,
    BrokerOpenOrderAdoption,
    BrokerOpenOrderAdoptionResult,
    BrokerSnapshot,
    HeldPosition,
    InternalOpenOrder,
    ReconciliationDiff,
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
        positions=(),
    )

    diffs = ReconciliationService().compare(
        now=now,
        snapshot=snapshot,
        internal_open_orders=(internal,),
        internal_positions=(),
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
        positions=(),
    )

    assert (
        ReconciliationService().compare(
            now=now,
            snapshot=snapshot,
            internal_open_orders=(internal,),
            internal_positions=(),
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
        positions=(),
    )

    diffs = ReconciliationService().compare(
        now=now,
        snapshot=snapshot,
        internal_open_orders=(),
        internal_positions=(),
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
        BrokerSnapshot(uuid4(), account_id, False, now + timedelta(minutes=1), (), ())
    )
    store = _RunStore()

    run = await ReconciliationService().run(
        now=now,
        account_id=account_id,
        reader=reader,
        store=store,
        internal_open_orders=(),
        internal_positions=(),
    )

    assert not run.complete and not run.succeeded
    assert store.runs == [run]
    assert run.diffs[0].kind is ReconciliationDiffKind.SNAPSHOT_INCOMPLETE


def _snapshot(
    positions: tuple[HeldPosition, ...], now: datetime, account_id: UUID | None = None
) -> BrokerSnapshot:
    return BrokerSnapshot(
        broker_id=uuid4(),
        account_id=account_id or uuid4(),
        complete=True,
        expires_at=now + timedelta(minutes=1),
        open_orders=(),
        positions=positions,
    )


def _position_diffs(
    broker: tuple[HeldPosition, ...], internal: tuple[HeldPosition, ...]
) -> tuple[ReconciliationDiff, ...]:
    now = datetime.now(UTC)
    return ReconciliationService().compare(
        now=now,
        snapshot=_snapshot(broker, now),
        internal_open_orders=(),
        internal_positions=internal,
    )


def test_positions_that_agree_are_not_a_difference() -> None:
    instrument_id = uuid4()
    held = (HeldPosition(instrument_id=instrument_id, quantity=Decimal("3")),)

    assert _position_diffs(held, held) == ()


def test_a_position_the_broker_does_not_report_blocks() -> None:
    instrument_id = uuid4()

    diffs = _position_diffs(
        (), (HeldPosition(instrument_id=instrument_id, quantity=Decimal("3")),)
    )

    assert [diff.kind for diff in diffs] == [
        ReconciliationDiffKind.INTERNAL_POSITION_BROKER_MISSING
    ]
    assert diffs[0].blocking
    assert diffs[0].instrument_id == instrument_id


def test_a_position_only_the_broker_knows_about_blocks() -> None:
    instrument_id = uuid4()

    diffs = _position_diffs(
        (HeldPosition(instrument_id=instrument_id, quantity=Decimal("3")),), ()
    )

    assert [diff.kind for diff in diffs] == [
        ReconciliationDiffKind.BROKER_POSITION_INTERNAL_MISSING
    ]
    assert diffs[0].instrument_id == instrument_id


def test_a_quantity_that_does_not_match_blocks() -> None:
    instrument_id = uuid4()

    diffs = _position_diffs(
        (HeldPosition(instrument_id=instrument_id, quantity=Decimal("3")),),
        (HeldPosition(instrument_id=instrument_id, quantity=Decimal("2")),),
    )

    # Every size calculated from here would be wrong, so this is not a warning.
    assert [diff.kind for diff in diffs] == [
        ReconciliationDiffKind.POSITION_QUANTITY_MISMATCH
    ]
    assert diffs[0].blocking
    assert diffs[0].instrument_id == instrument_id


def test_a_side_that_disagrees_is_a_mismatch_not_a_match() -> None:
    instrument_id = uuid4()

    diffs = _position_diffs(
        (HeldPosition(instrument_id=instrument_id, quantity=Decimal("3")),),
        (HeldPosition(instrument_id=instrument_id, quantity=Decimal("-3")),),
    )

    assert [diff.kind for diff in diffs] == [
        ReconciliationDiffKind.POSITION_QUANTITY_MISMATCH
    ]


def test_a_flat_instrument_is_absent_rather_than_zero() -> None:
    # Otherwise "holds nothing" and "was never looked at" become the same row.
    with pytest.raises(ValueError, match="cannot be zero"):
        HeldPosition(instrument_id=uuid4(), quantity=Decimal(0))


def test_a_snapshot_reports_each_instrument_once() -> None:
    instrument_id = uuid4()
    now = datetime.now(UTC)

    with pytest.raises(ValueError, match="each instrument once"):
        _snapshot(
            (
                HeldPosition(instrument_id=instrument_id, quantity=Decimal("1")),
                HeldPosition(instrument_id=instrument_id, quantity=Decimal("2")),
            ),
            now,
        )


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

    async def read_snapshot(
        self, *, account_id: object, now: datetime
    ) -> BrokerSnapshot:
        del account_id, now
        return self._snapshot


class _RunStore:
    def __init__(self) -> None:
        self.runs: list[ReconciliationRun] = []

    async def persist_run(self, run: ReconciliationRun) -> ReconciliationRun:
        self.runs.append(run)
        return run


def test_a_broker_that_does_not_echo_our_client_id_still_matches() -> None:
    """KIS has no client order id field and Toss omits it from its order list.
    Keying on the pair would report every one of their orders twice: once as
    ours the broker never heard of, once as theirs we never placed."""
    now = datetime(2026, 8, 27, tzinfo=UTC)
    snapshot = BrokerSnapshot(
        broker_id=uuid4(),
        account_id=uuid4(),
        complete=True,
        expires_at=now + timedelta(seconds=30),
        open_orders=(
            BrokerOpenOrder(
                broker_order_id="KIS-KRX:20260827:00001:0000000001",
                broker_client_order_id=None,
                canonical_terms_hash=b"a" * 32,
            ),
        ),
        positions=(),
    )
    internal = (
        InternalOpenOrder(
            order_id=uuid4(),
            broker_order_id="KIS-KRX:20260827:00001:0000000001",
            broker_client_order_id="ours-1",
        ),
    )

    diffs = ReconciliationService().compare(
        now=now,
        snapshot=snapshot,
        internal_open_orders=internal,
        internal_positions=(),
    )

    assert diffs == ()


def test_a_client_id_the_broker_does_not_recognise_is_one_finding() -> None:
    """One order id, two client ids: something placed an order on this account
    that we did not, and it took an id we did."""
    now = datetime(2026, 8, 27, tzinfo=UTC)
    snapshot = BrokerSnapshot(
        broker_id=uuid4(),
        account_id=uuid4(),
        complete=True,
        expires_at=now + timedelta(seconds=30),
        open_orders=(
            BrokerOpenOrder(
                broker_order_id="BINANCE-USDM:42",
                broker_client_order_id="somebody-elses",
                canonical_terms_hash=b"a" * 32,
            ),
        ),
        positions=(),
    )
    order_id = uuid4()
    internal = (
        InternalOpenOrder(
            order_id=order_id,
            broker_order_id="BINANCE-USDM:42",
            broker_client_order_id="ours-1",
        ),
    )

    diffs = ReconciliationService().compare(
        now=now,
        snapshot=snapshot,
        internal_open_orders=internal,
        internal_positions=(),
    )

    assert len(diffs) == 1
    assert diffs[0].kind is ReconciliationDiffKind.OPEN_ORDER_CLIENT_ID_MISMATCH
    assert diffs[0].blocking is True
    assert diffs[0].internal_order_id == order_id
