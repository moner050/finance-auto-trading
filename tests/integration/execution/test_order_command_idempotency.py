from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid7

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.domain.enums import IntentType, OrderStyle, Side
from autotrader.execution.intents.models import IntentOrigin, OrderIntent
from autotrader.execution.orders.service import (
    OrderService,
    OrderSubmissionContext,
)
from autotrader.execution.reconciliation.models import BrokerOpenOrderAdoption
from autotrader.persistence.mysql.adoption_store import (
    MySqlBrokerOpenOrderAdoptionStore,
)
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.accounts import (
    Account,
    AccountSnapshot,
    Broker,
)
from autotrader.persistence.mysql.models.core import (
    CoreExchange,
    CoreInstrument,
    CoreMarket,
)
from autotrader.persistence.mysql.models.events import OpsOutboxEvent
from autotrader.persistence.mysql.models.intents import (
    PersistedOrderIntent,
    PersistedRiskDecision,
    PersistedRiskReservation,
)
from autotrader.persistence.mysql.models.operations import OpsAuditLog
from autotrader.persistence.mysql.models.orders import (
    PersistedOrder,
    PersistedOrderCommand,
    PersistedOrderCommandAuthority,
    PersistedOrderEvent,
)
from autotrader.persistence.mysql.models.reconciliation import (
    PersistedReconciliationDiff,
    PersistedReconciliationRun,
)
from autotrader.persistence.mysql.models.risk import (
    RiskBudgetAnchor,
    RiskPolicy,
    RiskPolicyVersion,
    RiskSnapshot,
)
from autotrader.persistence.mysql.repositories.orders import MySqlOrderStore
from autotrader.risk.models import RiskDecision, RiskOutcome

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 8, 9, tzinfo=UTC)


@pytest.mark.integration
def test_replayed_approval_creates_one_order_submit_event_and_outbox() -> None:
    url = os.environ.get("DATABASE_URL")
    if url is None:
        pytest.skip("DATABASE_URL is required for MySQL integration tests")
    command.upgrade(Config(ROOT / "alembic.ini"), "head")

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            intent, decision = await _seed(sessions)
            submission = OrderSubmissionContext(
                broker_client_order_id=f"order-{intent.id.hex}",
                owner_runtime_instance_id=uuid7(),
                fencing_token=1,
                not_after=NOW + timedelta(minutes=1),
                time_in_force="DAY",
                authority_class="SUBMIT_NEW_EXPOSURE",
                created_at=NOW,
            )
            async with sessions() as session:
                service = OrderService(store=MySqlOrderStore(session))
                for _ in range(100):
                    await service.create_from_risk_decision(
                        decision=decision, intent=intent, submission=submission
                    )
                await session.commit()

            async with sessions() as session:
                assert await session.scalar(select(func.count(PersistedOrder.id))) == 1
                assert (
                    await session.scalar(select(func.count(PersistedOrderCommand.id)))
                    == 1
                )
                assert (
                    await session.scalar(select(func.count(PersistedOrderEvent.id)))
                    == 1
                )
                assert await session.scalar(select(func.count(OpsOutboxEvent.id))) == 1
        finally:
            await engine.dispose()

    asyncio.run(verify())


@pytest.mark.integration
def test_adopted_broker_open_order_is_idempotent_and_cannot_submit_new_exposure() -> (
    None
):
    url = os.environ.get("DATABASE_URL")
    if url is None:
        pytest.skip("DATABASE_URL is required for MySQL integration tests")
    command.upgrade(Config(ROOT / "alembic.ini"), "head")

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            intent, decision = await _seed(sessions)
            async with sessions() as session:
                account = await session.get(Account, intent.account_id)
                diff = await session.get(PersistedReconciliationDiff, intent.source_id)
                assert account is not None
                assert diff is not None
                global_anchor = await session.scalar(
                    select(RiskBudgetAnchor).where(
                        RiskBudgetAnchor.scope_type == "GLOBAL",
                        RiskBudgetAnchor.scope_key == "GLOBAL",
                        RiskBudgetAnchor.currency == "USD",
                    )
                )
                if global_anchor is None:
                    global_anchor = RiskBudgetAnchor(
                        id=uuid7(),
                        scope_type="GLOBAL",
                        scope_key="GLOBAL",
                        currency="USD",
                        position_risk_amount=Decimal("0"),
                        remaining_reservation_amount=Decimal("0"),
                        hard_limit_amount=Decimal("100"),
                        row_version=1,
                    )
                    session.add(global_anchor)
                account_anchor = RiskBudgetAnchor(
                    id=uuid7(),
                    scope_type="ACCOUNT",
                    scope_key=str(account.id),
                    currency="USD",
                    position_risk_amount=Decimal("0"),
                    remaining_reservation_amount=Decimal("0"),
                    hard_limit_amount=Decimal("100"),
                    row_version=1,
                )
                session.add(account_anchor)
                await session.flush()
                global_before = global_anchor.remaining_reservation_amount
                adoption = BrokerOpenOrderAdoption(
                    reconciliation_diff_id=diff.id,
                    account_id=account.id,
                    broker_id=account.broker_id,
                    broker_order_id=diff.broker_order_id or "missing-broker-order",
                    broker_client_order_id=f"adopted-{diff.id.hex}",
                    instrument_id=intent.instrument_id,
                    side=Side.BUY,
                    order_style=OrderStyle.LIMIT,
                    requested_quantity=Decimal("2"),
                    limit_price=Decimal("10"),
                    currency="USD",
                    reserved_risk_amount=Decimal("2"),
                    policy_version_id=decision.policy_version_id,
                    risk_snapshot_id=decision.risk_snapshot_id,
                    observed_at=NOW,
                    reservation_expires_at=NOW + timedelta(minutes=5),
                    payload_hash=b"a" * 32,
                )
                store = MySqlBrokerOpenOrderAdoptionStore(session)
                created = await store.adopt_open_order(adoption)
                assert created.created is True
                assert global_anchor.remaining_reservation_amount == global_before + 2
                assert account_anchor.remaining_reservation_amount == 2
                await session.commit()

            async with sessions() as session:
                replayed = await MySqlBrokerOpenOrderAdoptionStore(
                    session
                ).adopt_open_order(adoption)
                assert replayed.order_id == created.order_id
                assert replayed.reservation_id == created.reservation_id
                assert replayed.broker_order_id == created.broker_order_id
                assert replayed.created is False
                await session.commit()

            async with sessions() as session:
                order = await session.get(PersistedOrder, created.order_id)
                diff = await session.get(
                    PersistedReconciliationDiff, adoption.reconciliation_diff_id
                )
                assert order is not None
                assert diff is not None
                assert order.status == "UNKNOWN"
                assert diff.status == "RESOLVED"
                global_anchor = await session.scalar(
                    select(RiskBudgetAnchor).where(
                        RiskBudgetAnchor.scope_type == "GLOBAL",
                        RiskBudgetAnchor.scope_key == "GLOBAL",
                        RiskBudgetAnchor.currency == "USD",
                    )
                )
                account_anchor = await session.scalar(
                    select(RiskBudgetAnchor).where(
                        RiskBudgetAnchor.scope_type == "ACCOUNT",
                        RiskBudgetAnchor.scope_key == str(adoption.account_id),
                        RiskBudgetAnchor.currency == "USD",
                    )
                )
                assert global_anchor is not None
                assert account_anchor is not None
                assert global_anchor.remaining_reservation_amount == global_before + 2
                assert account_anchor.remaining_reservation_amount == 2
                assert (
                    await session.scalar(
                        select(func.count(PersistedRiskReservation.id)).where(
                            PersistedRiskReservation.id == created.reservation_id
                        )
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        select(func.count(PersistedOrderCommand.id)).where(
                            PersistedOrderCommand.order_id == order.id
                        )
                    )
                    == 0
                )
                authorities = list(
                    (
                        await session.scalars(
                            select(
                                PersistedOrderCommandAuthority.authority_class
                            ).where(PersistedOrderCommandAuthority.order_id == order.id)
                        )
                    ).all()
                )
                assert authorities == ["CANCEL"]
                assert (
                    await session.scalar(
                        select(func.count(OpsAuditLog.id)).where(
                            OpsAuditLog.action == "BROKER_OPEN_ORDER_ADOPTED",
                            OpsAuditLog.scope_key == str(order.id),
                        )
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        select(func.count(OpsOutboxEvent.id)).where(
                            OpsOutboxEvent.aggregate_id == order.id,
                            OpsOutboxEvent.aggregate_version == 1,
                        )
                    )
                    == 1
                )

                session.add(
                    PersistedOrderCommand(
                        id=uuid7(),
                        order_id=order.id,
                        account_id=order.account_id,
                        instrument_id=order.instrument_id,
                        command_type="SUBMIT",
                        command_sequence=1,
                        target_aggregate_version=1,
                        idempotency_key=f"illegal-submit:{order.id.hex}",
                        canonical_payload_hash=b"x" * 32,
                        broker_client_order_id=order.broker_client_order_id,
                        target_broker_order_id=None,
                        replaces_command_id=None,
                        origin_type="RECONCILIATION",
                        authority_class="SUBMIT_NEW_EXPOSURE",
                        owner_runtime_instance_id=None,
                        fencing_token=0,
                        not_after=NOW + timedelta(minutes=1),
                        side=order.side,
                        order_style=order.order_style,
                        quantity=order.requested_quantity,
                        limit_price=order.limit_price,
                        time_in_force="DAY",
                        status="PENDING",
                        dispatch_attempted_at=None,
                        result_state=None,
                    )
                )
                with pytest.raises(IntegrityError):
                    await session.flush()
                await session.rollback()

            async with sessions() as session:
                assert (
                    await session.scalar(
                        select(func.count(PersistedOrderCommand.id)).where(
                            PersistedOrderCommand.order_id == created.order_id
                        )
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(verify())


async def _seed(
    sessions: async_sessionmaker[object],
) -> tuple[OrderIntent, RiskDecision]:
    async with sessions() as session:
        broker = Broker(id=uuid7(), code=f"B-{uuid7().hex}", name="Test broker")
        market = CoreMarket(
            id=uuid7(),
            code=f"M{uuid7().hex[-12:]}",
            name="Test market",
            status="ACTIVE",
        )
        session.add_all([broker, market])
        await session.flush()
        exchange = CoreExchange(
            id=uuid7(),
            market_id=market.id,
            code=f"E{uuid7().hex[-12:]}",
            name="Test exchange",
            status="ACTIVE",
        )
        account = Account(
            id=uuid7(),
            broker_id=broker.id,
            account_alias=f"a-{uuid7().hex}",
            environment="PAPER",
            secret_reference="secret://order-test",
            enabled=True,
        )
        policy = RiskPolicy(id=uuid7(), code=f"p-{uuid7().hex}", active=True)
        session.add_all([exchange, account, policy])
        await session.flush()
        instrument = CoreInstrument(
            id=uuid7(),
            exchange_id=exchange.id,
            code=f"I{uuid7().hex[-12:]}",
            name="Test instrument",
            instrument_type="EQUITY",
            status="ACTIVE",
        )
        snapshot = AccountSnapshot(
            id=uuid7(), account_id=account.id, as_of=NOW, currency="USD"
        )
        version = RiskPolicyVersion(
            id=uuid7(),
            policy_id=policy.id,
            version="1",
            active=True,
            max_total_risk=Decimal("100"),
            max_position_value=Decimal("100"),
            max_daily_loss=Decimal("100"),
            max_drawdown=Decimal("100"),
        )
        session.add_all([instrument, snapshot, version])
        await session.flush()
        risk_snapshot = RiskSnapshot(
            id=uuid7(),
            account_snapshot_id=snapshot.id,
            account_id=account.id,
            as_of=NOW,
            currency="USD",
            equity=Decimal("100"),
            cash=Decimal("100"),
            gross_exposure=Decimal("0"),
            net_exposure=Decimal("0"),
            open_risk=Decimal("0"),
            daily_realized_pnl=Decimal("0"),
            daily_unrealized_pnl=Decimal("0"),
            drawdown=Decimal("0"),
            position_hash=b"p" * 32,
            open_order_hash=b"o" * 32,
        )
        session.add(risk_snapshot)
        await session.flush()
        reconciliation_run = PersistedReconciliationRun(
            id=uuid7(),
            broker_id=broker.id,
            account_id=account.id,
            started_at=NOW,
            completed_at=NOW,
            status="SUCCEEDED",
            snapshot_hash=b"r" * 32,
            complete=True,
        )
        reconciliation_diff = PersistedReconciliationDiff(
            id=uuid7(),
            run_id=reconciliation_run.id,
            internal_order_id=None,
            broker_order_id="legacy-order",
            broker_execution_id=None,
            diff_key="legacy-reconciliation-request",
            severity="BLOCKING",
            status="OPEN",
            expected_hash=b"e" * 32,
            observed_hash=b"o" * 32,
            created_at=NOW,
            resolved_at=None,
        )
        session.add(reconciliation_run)
        await session.flush()
        session.add(reconciliation_diff)
        await session.flush()
        intent_id = uuid7()
        persisted_intent = PersistedOrderIntent(
            id=intent_id,
            origin_type="RECONCILIATION",
            idempotency_key=f"reconciliation:{intent_id.hex}",
            canonical_payload_hash=b"i" * 32,
            account_id=account.id,
            instrument_id=instrument.id,
            intent_type="ENTRY",
            side="BUY",
            order_style="LIMIT",
            requested_quantity=Decimal("1"),
            limit_price=Decimal("10"),
            strategy_signal_id=None,
            protection_position_id=None,
            protection_reason_code=None,
            operator_audit_id=None,
            reconciliation_diff_id=reconciliation_diff.id,
            created_at=NOW,
        )
        decision_id = uuid7()
        persisted_decision = PersistedRiskDecision(
            id=decision_id,
            order_intent_id=intent_id,
            policy_version_id=version.id,
            risk_snapshot_id=risk_snapshot.id,
            outcome="APPROVE",
            requested_quantity=Decimal("1"),
            approved_quantity=Decimal("1"),
            approved_limit_price=Decimal("10"),
            reserved_risk_amount=Decimal("0"),
            currency="USD",
            reason_codes=[],
            decision_hash=b"d" * 32,
            decided_at=NOW,
        )
        session.add_all([persisted_intent, persisted_decision])
        await session.commit()
    return (
        OrderIntent(
            id=intent_id,
            origin=IntentOrigin.RECONCILIATION,
            source_id=reconciliation_diff.id,
            account_id=account.id,
            instrument_id=instrument.id,
            intent_type=IntentType.ENTRY,
            side=Side.BUY,
            order_style=OrderStyle.LIMIT,
            quantity=Decimal("1"),
            limit_price=Decimal("10"),
            idempotency_key=persisted_intent.idempotency_key,
        ),
        RiskDecision(
            id=decision_id,
            order_intent_id=intent_id,
            risk_snapshot_id=risk_snapshot.id,
            outcome=RiskOutcome.APPROVE,
            requested_quantity=Decimal("1"),
            reason_codes=(),
            approved_quantity=Decimal("1"),
            approved_limit_price=Decimal("10"),
            reserved_risk_amount=Decimal("0"),
            currency="USD",
            policy_version_id=version.id,
            decided_at=NOW,
            decision_hash=b"d" * 32,
        ),
    )
