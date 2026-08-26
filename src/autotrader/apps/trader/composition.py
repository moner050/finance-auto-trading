"""MySQL-backed implementations of the tick's ports.

The tick decides; these carry the decision to storage and to a broker. Each
one is deliberately narrow, because the loop should be readable as the five
steps it performs rather than as the plumbing underneath them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.domain.enums import OrderStyle
from autotrader.execution.dispatch.service import BrokerSubmitter, DispatchService
from autotrader.execution.intents.models import (
    AccountCandidate,
    OrderIntent,
    SizingApproved,
)
from autotrader.execution.intents.service import OrderIntentFactory
from autotrader.execution.orders.service import OrderService, OrderSubmissionContext
from autotrader.integrations.brokers.internal_paper import InternalPaperBroker
from autotrader.integrations.brokers.paper_submitter import (
    ExecutionBars,
    resolve_paper_fills,
)
from autotrader.persistence.mysql.dispatch_store import MySqlDispatchStore
from autotrader.persistence.mysql.models.intents import (
    PersistedOrderIntent,
    PersistedRiskDecision,
    PersistedRiskReservation,
)
from autotrader.persistence.mysql.models.operations import OpsTradingControl
from autotrader.persistence.mysql.models.orders import PersistedOrderCommand
from autotrader.persistence.mysql.models.strategy import (
    StrategyFeatureSchema,
    StrategyFeatureSnapshot,
    StrategySetup,
)
from autotrader.persistence.mysql.paper_journal import MySqlPaperJournal
from autotrader.persistence.mysql.repositories.david_v6 import DavidV6Repository
from autotrader.persistence.mysql.repositories.intents import OrderIntentRepository
from autotrader.persistence.mysql.repositories.operations import (
    RuntimeControlRepository,
)
from autotrader.persistence.mysql.repositories.orders import MySqlOrderStore
from autotrader.persistence.mysql.repositories.risk import MySqlRiskReservationUow
from autotrader.risk.models import RiskDecision, RiskOutcome
from autotrader.risk.service import (
    RiskDecisionRecord,
    RiskReservationRecord,
    RiskReservationService,
    RiskReservationUow,
)
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc
from autotrader.strategies.common.decisions import StrategyDecision
from autotrader.strategies.david_v6.hlit import HlitSetup
from autotrader.strategies.david_v6.models import V6Decision

NO_KILL_SWITCH = "NONE"
_RESERVATION_WINDOW = timedelta(minutes=5)


class MySqlTradingControl:
    """Trading is armed only when every stored control says so."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def is_armed(self) -> bool:
        async with self._sessions() as session:
            controls = (await session.scalars(select(OpsTradingControl))).all()
        # No control row means nobody armed anything, which is not armed. A
        # kill switch at any scope outranks an armed flag anywhere.
        return bool(controls) and all(
            control.armed and control.kill_switch_level == NO_KILL_SWITCH
            for control in controls
        )


class MySqlDecisionRecorder:
    """Every evaluation is written, tradeable or not."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(self, decision: V6Decision) -> None:
        async with self._sessions() as session:
            # persist_decision checks the setup and snapshot the decision
            # names, so those rows have to exist before it runs.
            await _persist_provenance(session, decision)
            await DavidV6Repository(session).persist_decision(decision)
            await session.commit()


async def _persist_provenance(session: AsyncSession, decision: V6Decision) -> None:
    """Create the setup and feature snapshot rows the decision refers to."""
    version_id = decision.strategy_version_id
    if await session.get(StrategySetup, decision.setup_id) is None:
        session.add(
            StrategySetup(
                id=decision.setup_id,
                strategy_version_id=version_id,
                status="ACTIVE",
            )
        )
    if await session.get(StrategyFeatureSnapshot, decision.feature_snapshot_id) is None:
        schema_id = new_uuid7()
        session.add(
            StrategyFeatureSchema(
                id=schema_id,
                strategy_version_id=version_id,
                schema_hash=_digest(f"schema:{version_id.hex}"),
            )
        )
        await session.flush()
        session.add(
            StrategyFeatureSnapshot(
                id=decision.feature_snapshot_id,
                feature_schema_id=schema_id,
                payload_hash=_digest(f"snapshot:{decision.id.hex}"),
                available_at=decision.completed_evidence_at,
            )
        )
    await session.flush()


@dataclass(frozen=True, slots=True)
class ExecutionAccount:
    """The configured account one loop trades for."""

    account: AccountCandidate
    policy_version_id: UUID
    risk_snapshot_id: UUID
    currency: str
    runtime_instance_id: UUID
    fencing_token: int

    def __post_init__(self) -> None:
        if type(cast(object, self.account)) is not AccountCandidate:
            raise TypeError("account must be an exact AccountCandidate")
        if type(self.currency) is not str or len(self.currency) != 3:
            raise ValueError("currency must be a three letter code")
        if type(self.fencing_token) is not int or self.fencing_token <= 0:
            raise ValueError("fencing_token must be a positive integer")


class MySqlPaperExecution:
    """Carries a tradeable decision to an order and across the broker boundary."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        account: ExecutionAccount,
        broker: BrokerSubmitter,
    ) -> None:
        self._sessions = sessions
        self._account = account
        self._broker = broker

    async def submit(
        self,
        *,
        decision: V6Decision,
        strategy_decision: StrategyDecision,
        setup: HlitSetup,
        now: datetime,
    ) -> UUID | None:
        del setup  # The drawn levels reach position management, not the order.
        moment = require_utc(now)
        command_id = await self._create_order(decision, strategy_decision, moment)
        if command_id is None:
            return None
        async with self._sessions() as session:
            await DispatchService(
                store=MySqlDispatchStore(session), broker=self._broker
            ).dispatch(command_id=command_id, now=moment)
            await session.commit()
        return command_id

    async def _create_order(
        self,
        decision: V6Decision,
        strategy_decision: StrategyDecision,
        now: datetime,
    ) -> UUID | None:
        async with self._sessions() as session:
            intent = OrderIntentFactory().from_strategy_decision(
                decision=strategy_decision,
                account=self._account.account,
                sizing=SizingApproved(quantity=decision.calculated_quantity),
            )
            # persist_decision already wrote the signal for a tradeable
            # decision, and it carries the decision's own id.
            stored = await OrderIntentRepository(session).create_or_get(
                _persisted_intent(intent, decision.id, now)
            )
            risk_decision = _risk_decision(
                decision=decision,
                intent_id=stored.id,
                account=self._account,
                now=now,
            )
            await RiskReservationService(
                uow=cast(RiskReservationUow, MySqlRiskReservationUow(session))
            ).persist_approval(
                decision=cast(RiskDecisionRecord, risk_decision),
                reservation=cast(
                    RiskReservationRecord,
                    _reservation(risk_decision, self._account.account.id, now),
                ),
                account_id=self._account.account.id,
                currency=self._account.currency,
            )
            order = await OrderService(
                store=MySqlOrderStore(session)
            ).create_from_risk_decision(
                decision=_domain_decision(risk_decision, stored.id),
                intent=intent,
                submission=OrderSubmissionContext(
                    broker_client_order_id=f"v6-{decision.id.hex}",
                    owner_runtime_instance_id=self._account.runtime_instance_id,
                    fencing_token=self._account.fencing_token,
                    not_after=now + _RESERVATION_WINDOW,
                    time_in_force="DAY",
                    authority_class="SUBMIT_NEW_EXPOSURE",
                    created_at=now,
                ),
            )
            await session.commit()
        if order is None:
            return None
        async with self._sessions() as session:
            return await session.scalar(
                select(PersistedOrderCommand.id).where(
                    PersistedOrderCommand.order_id == order.id
                )
            )


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _canonical_hash(intent: OrderIntent) -> bytes:
    payload = {
        "account_id": intent.account_id.hex,
        "instrument_id": intent.instrument_id.hex,
        "intent_type": intent.intent_type.value,
        "side": intent.side.value,
        "order_style": intent.order_style.value,
        "quantity": format(intent.quantity.normalize(), "f"),
        "limit_price": (
            None
            if intent.limit_price is None
            else format(intent.limit_price.normalize(), "f")
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).digest()


def _persisted_intent(
    intent: OrderIntent, signal_id: UUID, now: datetime
) -> PersistedOrderIntent:
    return PersistedOrderIntent(
        id=intent.id,
        origin_type=intent.origin.value,
        idempotency_key=intent.idempotency_key,
        canonical_payload_hash=_canonical_hash(intent),
        account_id=intent.account_id,
        instrument_id=intent.instrument_id,
        intent_type=intent.intent_type.value,
        side=intent.side.value,
        order_style=intent.order_style.value,
        requested_quantity=intent.quantity,
        limit_price=intent.limit_price,
        strategy_signal_id=signal_id,
        legacy_strategy_link_id=None,
        protection_position_id=None,
        protection_reason_code=None,
        operator_audit_id=None,
        reconciliation_diff_id=None,
        created_at=now,
    )


def _risk_decision(
    *,
    decision: V6Decision,
    intent_id: UUID,
    account: ExecutionAccount,
    now: datetime,
) -> PersistedRiskDecision:
    assert decision.planned_entry is not None
    reserved = decision.risk_fraction * decision.calculated_quantity
    return PersistedRiskDecision(
        id=new_uuid7(),
        order_intent_id=intent_id,
        policy_version_id=account.policy_version_id,
        risk_snapshot_id=account.risk_snapshot_id,
        outcome="APPROVE",
        requested_quantity=decision.calculated_quantity,
        approved_quantity=decision.calculated_quantity,
        approved_limit_price=(
            decision.planned_entry if decision.order_style is OrderStyle.LIMIT else None
        ),
        reserved_risk_amount=reserved,
        currency=account.currency,
        reason_codes=[],
        decision_hash=decision.id.bytes + decision.id.bytes,
        decided_at=now,
    )


def _reservation(
    decision: PersistedRiskDecision, account_id: UUID, now: datetime
) -> PersistedRiskReservation:
    amount = decision.reserved_risk_amount
    return PersistedRiskReservation(
        id=new_uuid7(),
        risk_decision_id=decision.id,
        order_intent_id=decision.order_intent_id,
        account_id=account_id,
        initial_risk_amount=amount,
        consumed_risk_amount=Decimal(0),
        remaining_risk_amount=amount,
        released_risk_amount=Decimal(0),
        status="ACTIVE",
        expires_at=now + _RESERVATION_WINDOW,
        release_reason=None,
    )


def _domain_decision(row: PersistedRiskDecision, intent_id: UUID) -> RiskDecision:
    return RiskDecision(
        id=row.id,
        order_intent_id=intent_id,
        risk_snapshot_id=row.risk_snapshot_id,
        outcome=RiskOutcome(row.outcome),
        requested_quantity=row.requested_quantity,
        reason_codes=(),
        approved_quantity=row.approved_quantity,
        approved_limit_price=row.approved_limit_price,
        reserved_risk_amount=row.reserved_risk_amount,
        currency=row.currency,
        policy_version_id=row.policy_version_id,
        decided_at=require_utc(row.decided_at),
        decision_hash=row.decision_hash,
    )


__all__ = (
    "ExecutionAccount",
    "LeaseSettings",
    "MySqlDecisionRecorder",
    "MySqlFillSettlement",
    "MySqlPaperExecution",
    "MySqlSchedulerLease",
    "MySqlTradingControl",
)


@dataclass(frozen=True, slots=True)
class LeaseSettings:
    lease_name: str
    runtime_instance_id: UUID
    ttl: timedelta

    def __post_init__(self) -> None:
        if type(self.ttl) is not timedelta or self.ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")


class MySqlSchedulerLease:
    """Only the instance holding the named lease may trade the account."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: LeaseSettings,
    ) -> None:
        self._sessions = sessions
        self._settings = settings

    async def acquire(self, now: datetime) -> bool:
        moment = require_utc(now)
        async with self._sessions() as session:
            lease = await RuntimeControlRepository(
                session
            ).acquire_named_scheduler_lease(
                lease_name=self._settings.lease_name,
                runtime_instance_id=self._settings.runtime_instance_id,
                now=moment,
                lease_expires_at=moment + self._settings.ttl,
            )
            await session.commit()
        return lease is not None


class MySqlFillSettlement:
    """Resolve paper orders whose fill bar has closed since the last pass."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        bars: ExecutionBars,
    ) -> None:
        self._sessions = sessions
        self._bars = bars

    async def settle(self, now: datetime) -> int:
        require_utc(now)
        async with self._sessions() as session:
            journal = MySqlPaperJournal(session)
            receipts = await resolve_paper_fills(
                broker=InternalPaperBroker(
                    journal=journal, market_data=cast(Any, self._bars)
                ),
                journal=journal,
                bars=self._bars,
            )
            await session.commit()
        return len(receipts)
