from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid7

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.config.settings import Settings
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
from autotrader.persistence.mysql.models.intents import (
    PersistedOrderIntent,
    PersistedRiskDecision,
    PersistedRiskReservation,
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
from autotrader.persistence.mysql.repositories.intents import OrderIntentRepository
from autotrader.persistence.mysql.repositories.risk import MySqlRiskReservationUow
from autotrader.risk.service import RiskReservationService

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 8, 9, tzinfo=UTC)


@pytest.mark.integration
def test_concurrent_duplicate_and_distinct_reservations_are_serialized() -> None:
    url = os.environ.get("DATABASE_URL")
    if url is None:
        pytest.skip("DATABASE_URL is required for MySQL concurrency verification")
    command.upgrade(Config(ROOT / "alembic.ini"), "head")

    async def verify() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            ids = await _seed(sessions)
            duplicate_key = f"reconciliation:{uuid7().hex}:{ids.account_id.hex}"

            async def duplicate() -> UUID:
                async with sessions() as session:
                    intent = _intent(
                        id=uuid7(),
                        account_id=ids.account_id,
                        instrument_id=ids.instrument_id,
                        key=duplicate_key,
                        reconciliation_diff_id=ids.reconciliation_diff_id,
                    )
                    stored = await OrderIntentRepository(session).create_or_get(intent)
                    await session.commit()
                    return stored.id

            duplicate_ids = await asyncio.gather(*(duplicate() for _ in range(20)))
            assert len(set(duplicate_ids)) == 1

            intents = await _create_distinct_intents(sessions, ids)

            async def reserve(intent_id: UUID, decision_id: UUID) -> bool:
                async with sessions() as session:
                    decision = PersistedRiskDecision(
                        id=decision_id,
                        order_intent_id=intent_id,
                        policy_version_id=ids.policy_version_id,
                        risk_snapshot_id=ids.risk_snapshot_id,
                        outcome="APPROVE",
                        requested_quantity=Decimal("1"),
                        approved_quantity=Decimal("1"),
                        approved_limit_price=Decimal("300"),
                        reserved_risk_amount=Decimal("300"),
                        currency="USD",
                        reason_codes=[],
                        decision_hash=decision_id.bytes + b"x" * 16,
                        decided_at=NOW,
                    )
                    reservation = PersistedRiskReservation(
                        id=uuid7(),
                        risk_decision_id=decision_id,
                        order_intent_id=intent_id,
                        account_id=ids.account_id,
                        initial_risk_amount=Decimal("300"),
                        consumed_risk_amount=Decimal("0"),
                        remaining_risk_amount=Decimal("300"),
                        released_risk_amount=Decimal("0"),
                        status="ACTIVE",
                        expires_at=NOW + timedelta(minutes=1),
                        release_reason=None,
                    )
                    try:
                        await RiskReservationService(
                            uow=MySqlRiskReservationUow(session)
                        ).persist_approval(
                            decision=decision,
                            reservation=reservation,
                            account_id=ids.account_id,
                            currency="USD",
                        )
                        await session.commit()
                        return True
                    except ValueError:
                        await session.rollback()
                        return False

            outcomes = await asyncio.gather(
                reserve(intents[0], uuid7()), reserve(intents[1], uuid7())
            )
            assert sorted(outcomes) == [False, True]
            async with sessions() as session:
                anchors = list(
                    (
                        await session.scalars(
                            select(RiskBudgetAnchor).where(
                                RiskBudgetAnchor.currency == "USD"
                            )
                        )
                    ).all()
                )
                assert {anchor.remaining_reservation_amount for anchor in anchors} == {
                    Decimal("300")
                }
                assert (
                    await session.scalar(select(func.count(PersistedRiskDecision.id)))
                ) == 1
                assert (
                    await session.scalar(
                        select(func.count(PersistedRiskReservation.id))
                    )
                ) == 1
        finally:
            await engine.dispose()

    asyncio.run(verify())


class _Ids:
    def __init__(
        self,
        *,
        account_id: UUID,
        instrument_id: UUID,
        policy_version_id: UUID,
        risk_snapshot_id: UUID,
        reconciliation_diff_id: UUID,
    ) -> None:
        self.account_id = account_id
        self.instrument_id = instrument_id
        self.policy_version_id = policy_version_id
        self.risk_snapshot_id = risk_snapshot_id
        self.reconciliation_diff_id = reconciliation_diff_id


async def _seed(sessions: async_sessionmaker[object]) -> _Ids:
    async with sessions() as session:
        broker = Broker(id=uuid7(), code=f"RISK-{uuid7().hex}", name="Risk broker")
        market = CoreMarket(
            id=uuid7(),
            code=f"M{uuid7().hex[-12:]}",
            name="Risk market",
            status="ACTIVE",
        )
        exchange = CoreExchange(
            id=uuid7(),
            market_id=market.id,
            code=f"E{uuid7().hex[-12:]}",
            name="Risk exchange",
            status="ACTIVE",
        )
        instrument = CoreInstrument(
            id=uuid7(),
            exchange_id=exchange.id,
            code=f"I{uuid7().hex[-12:]}",
            name="Risk instrument",
            instrument_type="EQUITY",
            status="ACTIVE",
        )
        account = Account(
            id=uuid7(),
            broker_id=broker.id,
            account_alias=f"risk-{uuid7().hex}",
            environment="PAPER",
            secret_reference="secret://risk",
            enabled=True,
        )
        snapshot = AccountSnapshot(
            id=uuid7(), account_id=account.id, as_of=NOW, currency="USD"
        )
        policy = RiskPolicy(id=uuid7(), code=f"risk-{uuid7().hex}", active=True)
        version = RiskPolicyVersion(
            id=uuid7(),
            policy_id=policy.id,
            version="1",
            active=True,
            max_total_risk=Decimal("500"),
            max_position_value=Decimal("500"),
            max_daily_loss=Decimal("100"),
            max_drawdown=Decimal("100"),
        )
        risk_snapshot = RiskSnapshot(
            id=uuid7(),
            account_snapshot_id=snapshot.id,
            account_id=account.id,
            as_of=NOW,
            currency="USD",
            equity=Decimal("1000"),
            cash=Decimal("1000"),
            gross_exposure=Decimal("0"),
            net_exposure=Decimal("0"),
            open_risk=Decimal("0"),
            daily_realized_pnl=Decimal("0"),
            daily_unrealized_pnl=Decimal("0"),
            drawdown=Decimal("0"),
            position_hash=b"p" * 32,
            open_order_hash=b"o" * 32,
        )
        anchors = [
            RiskBudgetAnchor(
                id=uuid7(),
                scope_type="GLOBAL",
                scope_key="GLOBAL",
                currency="USD",
                position_risk_amount=Decimal("0"),
                remaining_reservation_amount=Decimal("0"),
                hard_limit_amount=Decimal("500"),
                row_version=1,
            ),
            RiskBudgetAnchor(
                id=uuid7(),
                scope_type="ACCOUNT",
                scope_key=str(account.id),
                currency="USD",
                position_risk_amount=Decimal("0"),
                remaining_reservation_amount=Decimal("0"),
                hard_limit_amount=Decimal("500"),
                row_version=1,
            ),
        ]
        session.add_all(
            [
                broker,
                market,
                exchange,
                instrument,
                account,
                snapshot,
                policy,
                version,
                risk_snapshot,
                *anchors,
            ]
        )
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
            broker_order_id="risk-test-order",
            broker_execution_id=None,
            diff_key="risk-test-reconciliation-request",
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
        await session.commit()
        return _Ids(
            account_id=account.id,
            instrument_id=instrument.id,
            policy_version_id=version.id,
            risk_snapshot_id=risk_snapshot.id,
            reconciliation_diff_id=reconciliation_diff.id,
        )


def _intent(
    *,
    id: UUID,
    account_id: UUID,
    instrument_id: UUID,
    key: str,
    reconciliation_diff_id: UUID,
) -> PersistedOrderIntent:
    return PersistedOrderIntent(
        id=id,
        origin_type="RECONCILIATION",
        idempotency_key=key,
        canonical_payload_hash=b"i" * 32,
        account_id=account_id,
        instrument_id=instrument_id,
        intent_type="ENTRY",
        side="BUY",
        order_style="LIMIT",
        requested_quantity=Decimal("1"),
        limit_price=Decimal("300"),
        strategy_signal_id=None,
        protection_position_id=None,
        protection_reason_code=None,
        operator_audit_id=None,
        reconciliation_diff_id=reconciliation_diff_id,
        created_at=NOW,
    )


async def _create_distinct_intents(
    sessions: async_sessionmaker[object], ids: _Ids
) -> tuple[UUID, UUID]:
    async with sessions() as session:
        first = _intent(
            id=uuid7(),
            account_id=ids.account_id,
            instrument_id=ids.instrument_id,
            key=f"reconciliation:{uuid7().hex}:{ids.account_id.hex}",
            reconciliation_diff_id=ids.reconciliation_diff_id,
        )
        second = _intent(
            id=uuid7(),
            account_id=ids.account_id,
            instrument_id=ids.instrument_id,
            key=f"reconciliation:{uuid7().hex}:{ids.account_id.hex}",
            reconciliation_diff_id=ids.reconciliation_diff_id,
        )
        session.add_all([first, second])
        await session.commit()
        return first.id, second.id
