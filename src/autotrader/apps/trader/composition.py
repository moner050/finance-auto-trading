"""MySQL-backed implementations of the tick's ports.

The tick decides; these carry the decision to storage and to a broker. Each
one is deliberately narrow, because the loop should be readable as the five
steps it performs rather than as the plumbing underneath them.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.trader.quotes import QuoteSource
from autotrader.domain.enums import IntentType, OrderStyle, Side
from autotrader.execution.controls.models import KillSwitchLevel
from autotrader.execution.dispatch.service import BrokerSubmitter, DispatchService
from autotrader.execution.fills.models import ChargeLegRole
from autotrader.execution.intents.models import (
    AccountCandidate,
    IntentOrigin,
    OrderIntent,
    OrderTerms,
    ProtectionRequest,
    SizingApproved,
)
from autotrader.execution.intents.service import OrderIntentFactory
from autotrader.execution.orders.models import CommandType
from autotrader.execution.orders.service import (
    NEW_EXPOSURE,
    STRICT_REDUCTION,
    OrderService,
    OrderSubmissionContext,
)
from autotrader.execution.reconciliation.models import (
    BrokerOpenOrder,
    BrokerSnapshot,
    HeldPosition,
)
from autotrader.execution.reconciliation.service import (
    BrokerSnapshotReader,
    ReconciliationService,
)
from autotrader.integrations.brokers.binance_usdm.orders import (
    binance_normal_client_order_id,
)
from autotrader.integrations.brokers.internal_paper import (
    InternalPaperBroker,
    PaperOrderReceipt,
)
from autotrader.integrations.brokers.paper_fills import (
    paper_broker_order_id,
    paper_execution_event,
)
from autotrader.integrations.brokers.paper_submitter import (
    ExecutionBars,
    resolve_paper_fills,
)
from autotrader.operations.david_v6_position import (
    V6ManagedPosition,
    V6PositionAction,
    V6PositionActionKind,
)
from autotrader.persistence.mysql.dispatch_store import MySqlDispatchStore
from autotrader.persistence.mysql.models.accounts import Account
from autotrader.persistence.mysql.models.david_v6 import DavidV6DecisionRow
from autotrader.persistence.mysql.models.intents import (
    PersistedOrderIntent,
    PersistedRiskDecision,
    PersistedRiskReservation,
)
from autotrader.persistence.mysql.models.operations import (
    OpsIncident,
    OpsTradingControl,
)
from autotrader.persistence.mysql.models.orders import (
    PersistedBrokerOrderLink,
    PersistedOrder,
    PersistedOrderCommand,
)
from autotrader.persistence.mysql.models.paper import PaperOrderRow
from autotrader.persistence.mysql.models.positions import Position
from autotrader.persistence.mysql.models.strategy import (
    StrategyFeatureSchema,
    StrategyFeatureSnapshot,
    StrategySetup,
)
from autotrader.persistence.mysql.paper_journal import MySqlPaperJournal
from autotrader.persistence.mysql.repositories.david_v6 import DavidV6Repository
from autotrader.persistence.mysql.repositories.david_v6_position import (
    MySqlManagedPositions,
)
from autotrader.persistence.mysql.repositories.fills import MySqlFillStore
from autotrader.persistence.mysql.repositories.intents import OrderIntentRepository
from autotrader.persistence.mysql.repositories.operations import (
    RuntimeControlRepository,
    trip_kill_switch,
)
from autotrader.persistence.mysql.repositories.orders import MySqlOrderStore
from autotrader.persistence.mysql.repositories.policy_binding import (
    AccountPolicyBindings,
)
from autotrader.persistence.mysql.repositories.protection import (
    ProtectionRepository,
)
from autotrader.persistence.mysql.repositories.reconciliation import (
    ReconciliationRepository,
)
from autotrader.persistence.mysql.repositories.risk import MySqlRiskReservationUow
from autotrader.risk.models import RiskDecision, RiskOutcome, V6RiskPolicySnapshot
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
from autotrader.strategies.david_v6.models import V6Decision, V6Market

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


POSITION_WITHOUT_PROTECTION = "POSITION_WITHOUT_PROTECTION"
BLOCK_NEW_EXPOSURE = "BLOCK_NEW_EXPOSURE"


class MySqlProtectionGuard:
    """Refuse to open more while something already held has no stop.

    The strategy's rule is that a position always has a protective stop behind
    it. Placing one is settlement's job; this is the check that the job was
    done, because an invariant nobody verifies is a comment.

    It blocks new exposure rather than halting outright. A full halt would
    also stop the protective order from being placed or filled, which is the
    opposite of what an unprotected position needs.
    """

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        account: ExecutionAccount,
    ) -> None:
        self._sessions = sessions
        self._account = account

    async def unprotected(self, now: datetime) -> int:
        moment = require_utc(now)
        async with self._sessions() as session:
            missing = await ProtectionRepository(session).unprotected_instruments(
                account_id=self._account.account.id
            )
            if not missing:
                return 0
            await self._raise_incidents(session, list(missing), moment)
            await _block_new_exposure(session)
            await session.commit()
        return len(missing)

    async def _raise_incidents(
        self, session: AsyncSession, missing: list[UUID], now: datetime
    ) -> None:
        for instrument_id in missing:
            scope_key = str(instrument_id)
            # One open incident per instrument: a loop that raises a new one
            # every pass buries the first report under its own repetition.
            existing = await session.scalar(
                select(OpsIncident.id).where(
                    OpsIncident.reason_code == POSITION_WITHOUT_PROTECTION,
                    OpsIncident.scope_type == "INSTRUMENT",
                    OpsIncident.scope_key == scope_key,
                    OpsIncident.status == "OPEN",
                )
            )
            if existing is not None:
                continue
            session.add(
                OpsIncident(
                    severity="BLOCKING",
                    status="OPEN",
                    reason_code=POSITION_WITHOUT_PROTECTION,
                    scope_type="INSTRUMENT",
                    scope_key=scope_key,
                    created_at=now,
                )
            )


async def _block_new_exposure(session: AsyncSession) -> None:
    """Stop opening exposure, and only that.

    A full halt would also stop a protective order from being placed or
    filled, which is the opposite of what an account in trouble needs.
    """
    controls = (await session.scalars(select(OpsTradingControl))).all()
    for control in controls:
        if control.kill_switch_level == NO_KILL_SWITCH:
            control.kill_switch_level = BLOCK_NEW_EXPOSURE
            control.row_version += 1


SNAPSHOT_WINDOW = timedelta(minutes=1)


class MySqlPaperSnapshotReader:
    """What the paper broker says the account holds.

    Its receipts are written by the broker; the position ledger is written by
    the fill store from broker execution events. They come from the same fill
    but along different paths, so a disagreement between them is a defect in
    one of those paths rather than a difference of opinion about the market.
    """

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        account: ExecutionAccount,
    ) -> None:
        self._sessions = sessions
        self._account = account

    async def read_snapshot(
        self, *, account_id: object, now: datetime
    ) -> BrokerSnapshot:
        moment = require_utc(now)
        if account_id != self._account.account.id:
            raise ValueError("this reader answers for one account only")
        async with self._sessions() as session:
            broker_id = await session.scalar(
                select(Account.broker_id).where(Account.id == self._account.account.id)
            )
            if broker_id is None:
                raise LookupError("the account is not bound to a broker")
            filled = (
                await session.execute(
                    select(
                        PersistedOrder.instrument_id,
                        PaperOrderRow.side,
                        func.sum(PaperOrderRow.filled_quantity),
                    )
                    .join(
                        PersistedOrder,
                        PersistedOrder.id == PaperOrderRow.order_id,
                    )
                    .where(
                        PersistedOrder.account_id == self._account.account.id,
                        PaperOrderRow.filled_quantity.is_not(None),
                    )
                    .group_by(PersistedOrder.instrument_id, PaperOrderRow.side)
                )
            ).all()
            # A staged command with no receipt is an order this broker is
            # still holding. Reporting only fills would make every working
            # order look like one the broker had never heard of.
            staged = (
                await session.execute(
                    select(
                        PaperOrderRow.command_id,
                        PersistedOrder.broker_client_order_id,
                        PaperOrderRow.command_digest,
                    )
                    .join(
                        PersistedOrder,
                        PersistedOrder.id == PaperOrderRow.order_id,
                    )
                    .where(
                        PersistedOrder.account_id == self._account.account.id,
                        PaperOrderRow.status.is_(None),
                    )
                    .order_by(PaperOrderRow.command_id)
                )
            ).all()
        return BrokerSnapshot(
            broker_id=broker_id,
            account_id=self._account.account.id,
            complete=True,
            expires_at=moment + SNAPSHOT_WINDOW,
            open_orders=tuple(
                BrokerOpenOrder(
                    broker_order_id=paper_broker_order_id(command_id),
                    broker_client_order_id=client_order_id,
                    canonical_terms_hash=digest,
                )
                for command_id, client_order_id, digest in staged
            ),
            positions=_net_positions([(row[0], row[1], row[2]) for row in filled]),
        )


def _net_positions(
    rows: Sequence[tuple[UUID, str, Decimal | None]],
) -> tuple[HeldPosition, ...]:
    """Buys less sells, per instrument, leaving out anything that nets flat."""
    net: dict[UUID, Decimal] = {}
    for instrument_id, side, filled in rows:
        if filled is None:
            continue
        signed = filled if Side(side) is Side.BUY else -filled
        net[instrument_id] = net.get(instrument_id, Decimal(0)) + signed
    return tuple(
        HeldPosition(instrument_id=instrument_id, quantity=quantity)
        for instrument_id, quantity in sorted(
            net.items(), key=lambda item: str(item[0])
        )
        if quantity != 0
    )


class MySqlReconciler:
    """Compare the account against the broker before deciding anything new."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        account: ExecutionAccount,
        reader: BrokerSnapshotReader,
    ) -> None:
        self._sessions = sessions
        self._account = account
        self._reader = reader

    async def reconcile(self, now: datetime) -> int:
        moment = require_utc(now)
        account_id = self._account.account.id
        async with self._sessions() as session:
            repository = ReconciliationRepository(session)
            run = await ReconciliationService().run(
                now=moment,
                account_id=account_id,
                reader=self._reader,
                store=repository,
                internal_open_orders=await repository.internal_open_orders(
                    account_id=account_id
                ),
                internal_positions=await repository.internal_positions(
                    account_id=account_id
                ),
            )
            blocking = sum(1 for diff in run.diffs if diff.blocking)
            if blocking:
                # persist_run already raised the incidents. What is left is to
                # stop opening exposure against numbers we cannot trust.
                await _block_new_exposure(session)
            await session.commit()
        return blocking


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


class UnboundAccountError(RuntimeError):
    """Raised when an account has no risk policy the loop can trade under."""


@dataclass(frozen=True, slots=True)
class BoundPolicy:
    """The policy an account is bound to, and the version it came from.

    The two travel together because they are the same fact. Handing the loop a
    snapshot separately from the version id it is recorded under is how a
    decision comes to be measured against one policy and filed under another.
    """

    policy_version_id: UUID
    snapshot: V6RiskPolicySnapshot

    def __post_init__(self) -> None:
        if self.snapshot.policy_version_id != self.policy_version_id:
            raise ValueError("the snapshot and the binding must name one version")


async def bound_policy(
    sessions: async_sessionmaker[AsyncSession],
    *,
    account_id: UUID,
    market: V6Market,
) -> BoundPolicy:
    """Read the account's policy from its binding, or refuse to start.

    Section 11.4 makes the binding the place an operator says which policy an
    account trades under. If the loop took its policy from whoever wired it,
    the screen would be recording a decision nothing acts on.
    """
    async with sessions() as session:
        snapshot = await AccountPolicyBindings(session).resolve(
            account_id, market=market
        )
        await session.rollback()
    if snapshot is None:
        raise UnboundAccountError(
            "this account has no active risk policy binding for its market"
        )
    return BoundPolicy(policy_version_id=snapshot.policy_version_id, snapshot=snapshot)


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
        quotes: QuoteSource,
    ) -> None:
        self._sessions = sessions
        self._account = account
        self._broker = broker
        self._quotes = quotes

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
            # A market order with no trigger has to know the price it
            # will get, and this is where that price comes from. §31.11.
            intent = OrderIntentFactory().from_strategy_decision(
                decision=strategy_decision,
                account=self._account.account,
                sizing=SizingApproved(quantity=decision.calculated_quantity),
                quote=await self._quotes.quote(),
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
            command_id = new_uuid7()
            # Chosen here rather than inside the factory: Binance
            # USD-M requires the client order id to name the command,
            # and recovery reconstructs it from the command alone.
            order = await OrderService(
                store=MySqlOrderStore(session)
            ).create_from_risk_decision(
                decision=_domain_decision(risk_decision, stored.id),
                intent=intent,
                submission=OrderSubmissionContext(
                    broker_client_order_id=binance_normal_client_order_id(command_id),
                    command_id=command_id,
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


STRUCTURAL_STOP = "STRUCTURAL_STOP"
REPLACE_NON_INCREASING = "REPLACE_NON_INCREASING"


@dataclass(frozen=True, slots=True)
class ProtectionPlan:
    """Everything the stop needs, read back from what the entry left behind."""

    position_id: UUID
    instrument_id: UUID
    entry_side: Side
    quantity: Decimal
    structural_stop: Decimal


async def protection_plan(
    session: AsyncSession, *, order_id: UUID, filled_quantity: Decimal
) -> ProtectionPlan | None:
    """The stop a filled entry needs, whoever settled it.

    Paper resolves fills against a bar and live reads them from the venue,
    but what has to happen next is the same, so this takes the two facts
    both of them have rather than one of their receipt types.
    """
    order = await session.get(PersistedOrder, order_id)
    if order is None:
        raise LookupError("a settled order has no canonical order")
    intent = await session.get(PersistedOrderIntent, order.order_intent_id)
    if intent is None:
        raise LookupError("a settled order has no intent")
    if IntentType(intent.intent_type) is not IntentType.ENTRY:
        # Only an entry opens exposure, so only an entry needs a stop behind
        # it. A protective fill closing one does not need protecting.
        return None
    if intent.strategy_signal_id is None:
        raise ValueError("an entry has no strategy signal to read a stop from")
    stop = await session.scalar(
        select(DavidV6DecisionRow.structural_stop).where(
            DavidV6DecisionRow.strategy_signal_id == intent.strategy_signal_id
        )
    )
    if stop is None:
        # The decision that opened this position named a stop, or it was never
        # tradeable. Either way, guessing one here would invent a risk limit
        # the strategy never agreed to.
        raise ValueError("a filled entry has no recorded structural stop")
    position = await session.scalar(
        select(Position).where(
            Position.account_id == order.account_id,
            Position.instrument_id == order.instrument_id,
        )
    )
    if position is None:
        raise LookupError("a settled entry left no position to protect")
    return ProtectionPlan(
        position_id=position.id,
        instrument_id=order.instrument_id,
        entry_side=Side(order.side),
        quantity=filled_quantity,
        structural_stop=stop,
    )


def _protection_intent(
    intent: OrderIntent, plan: ProtectionPlan, now: datetime
) -> PersistedOrderIntent:
    row = _persisted_intent(intent, plan.position_id, now)
    # The stop is owed to a position, not to a signal, and the row records
    # which one so an operator can see what it is protecting.
    row.strategy_signal_id = None
    row.protection_position_id = plan.position_id
    row.protection_reason_code = STRUCTURAL_STOP
    return row


def _reduction_decision(
    *,
    intent_id: UUID,
    quantity: Decimal,
    account: ExecutionAccount,
    now: datetime,
) -> PersistedRiskDecision:
    """A stop closes exposure, so it reserves nothing."""
    return PersistedRiskDecision(
        id=new_uuid7(),
        order_intent_id=intent_id,
        policy_version_id=account.policy_version_id,
        risk_snapshot_id=account.risk_snapshot_id,
        outcome="REDUCE",
        requested_quantity=quantity,
        approved_quantity=quantity,
        approved_limit_price=None,
        reserved_risk_amount=Decimal(0),
        currency=account.currency,
        reason_codes=[STRUCTURAL_STOP],
        decision_hash=_digest(f"stop:{intent_id.hex}"),
        decided_at=now,
    )


def _consumed_reservation(
    decision: PersistedRiskDecision, account_id: UUID, now: datetime
) -> PersistedRiskReservation:
    return PersistedRiskReservation(
        id=new_uuid7(),
        risk_decision_id=decision.id,
        order_intent_id=decision.order_intent_id,
        account_id=account_id,
        initial_risk_amount=Decimal(0),
        consumed_risk_amount=Decimal(0),
        remaining_risk_amount=Decimal(0),
        released_risk_amount=Decimal(0),
        status="CONSUMED",
        expires_at=now + _RESERVATION_WINDOW,
        release_reason=None,
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
    "LEG_ROLES",
    "BoundPolicy",
    "ExecutionAccount",
    "LeaseSettings",
    "MySqlDecisionRecorder",
    "MySqlFillSettlement",
    "MySqlPaperExecution",
    "MySqlSchedulerLease",
    "MySqlTradingControl",
    "ProtectionPlan",
    "UnboundAccountError",
    "bound_policy",
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


LEG_ROLES = {
    IntentType.ENTRY: ChargeLegRole.ENTRY,
    IntentType.EXIT: ChargeLegRole.EXIT_TARGET,
    IntentType.PROTECTIVE: ChargeLegRole.EXIT_STOP,
}


# The reason an exit order carries, so an operator reading the intent sees the
# rule that produced it rather than only that something closed.
_EXIT_KINDS = frozenset(
    {
        V6PositionActionKind.EXIT_FULL_FIB_66,
        V6PositionActionKind.EXIT_FULL_METODO_CROSS_DOWN,
        V6PositionActionKind.EXIT_FULL_BLOCKING_BIG_TRADE,
        V6PositionActionKind.EXIT_FULL_SESSION_CLOSE,
        V6PositionActionKind.EMERGENCY_EXIT_FULL,
    }
)


_STOP_KINDS = frozenset(
    {
        V6PositionActionKind.ACTIVATE_INITIAL_STOP,
        V6PositionActionKind.MOVE_STOP_TO_BREAK_EVEN,
    }
)


class PositionActionUnsupportedError(RuntimeError):
    """Raised for an action this sink cannot carry out.

    Loud on purpose. An action decided and then quietly dropped is the exact
    failure this whole path exists to fix, so anything not built yet stops the
    run and names itself rather than passing silently.
    """


async def create_protective_order(
    session: AsyncSession,
    *,
    account: ExecutionAccount,
    plan: ProtectionPlan,
    now: datetime,
) -> UUID | None:
    """Place a stop behind a position, from a fill or from a stop move.

    Module level because both reach it. The settlement hook places the
    first one when an entry fills; the position manager places one when it
    finds a position with nothing working behind it. Two copies of this
    would be two answers to what a protective order is.
    """
    intent = OrderIntentFactory().from_protection(
        account=account.account,
        request=ProtectionRequest(
            locked_position_id=plan.position_id,
            reason_code=STRUCTURAL_STOP,
            instrument_id=plan.instrument_id,
            intent_type=IntentType.PROTECTIVE,
            side=Side.SELL if plan.entry_side is Side.BUY else Side.BUY,
            order_style=OrderStyle.MARKET,
            terms=OrderTerms(
                requested_quantity=plan.quantity,
                limit_price=None,
                trigger_price=plan.structural_stop,
            ),
        ),
    )
    stored = await OrderIntentRepository(session).create_or_get(
        _protection_intent(intent, plan, now)
    )
    risk_decision = _reduction_decision(
        intent_id=stored.id,
        quantity=plan.quantity,
        account=account,
        now=now,
    )
    await RiskReservationService(
        uow=cast(RiskReservationUow, MySqlRiskReservationUow(session))
    ).persist_approval(
        decision=cast(RiskDecisionRecord, risk_decision),
        reservation=cast(
            RiskReservationRecord,
            _consumed_reservation(risk_decision, account.account.id, now),
        ),
        account_id=account.account.id,
        currency=account.currency,
    )
    command_id = new_uuid7()
    # Chosen here rather than inside the factory: Binance
    # USD-M requires the client order id to name the command,
    # and recovery reconstructs it from the command alone.
    order = await OrderService(
        store=MySqlOrderStore(session)
    ).create_from_risk_decision(
        decision=_domain_decision(risk_decision, stored.id),
        intent=intent,
        submission=OrderSubmissionContext(
            broker_client_order_id=binance_normal_client_order_id(command_id),
            command_id=command_id,
            owner_runtime_instance_id=account.runtime_instance_id,
            fencing_token=account.fencing_token,
            not_after=now + _RESERVATION_WINDOW,
            time_in_force="GTC",
            authority_class=STRICT_REDUCTION,
            created_at=now,
        ),
    )
    if order is None:
        return None
    await session.flush()
    return await session.scalar(
        select(PersistedOrderCommand.id).where(
            PersistedOrderCommand.order_id == order.id
        )
    )


class MySqlPositionActions:
    """Turn a decided action into an order, or refuse to pretend.

        What is built is the half that gets a position out: the four full exits,
        which are what `exit_before_blocking_big_trade` and the fibonacci target
        come to, plus the telemetry that only records a level was reached.

    Stops move by replacing, not by cancelling and placing again: the gap
        between a cancel and its replacement is a position with nothing behind it,
        which is what the stop was for.

        What is still not built is the add at thirty points. It increases exposure,
        so it needs a real risk reservation rather than the REDUCE a closing order
        carries, and leaving it out changes nothing about how a position behaves
        today because it never happened before either.

        An emergency exit closes first and refuses the halt second, in that order.
        The position is out because that is the urgent part, and the run then
        stops with its reason rather than carrying on with an account that was
        supposed to be halted and was not.
    """

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        account: ExecutionAccount,
        instrument_id: UUID,
        broker: BrokerSubmitter,
        quotes: QuoteSource,
    ) -> None:
        self._sessions = sessions
        self._account = account
        self._instrument_id = instrument_id
        self._broker = broker
        self._quotes = quotes

    async def apply(
        self,
        action: V6PositionAction,
        *,
        position: V6ManagedPosition,
        position_id: UUID,
        now: datetime,
    ) -> None:
        moment = require_utc(now)
        if action.telemetry_only:
            async with self._sessions() as session:
                await MySqlManagedPositions(session).record_mark(
                    position_id=position_id, mark=action.kind.value, now=moment
                )
                await session.commit()
            return
        if action.kind in _STOP_KINDS:
            await self._move_stop(
                action, position=position, position_id=position_id, now=moment
            )
            return
        if action.kind is V6PositionActionKind.ADD_AND_MOVE_STOP:
            await self._add(
                action, position=position, position_id=position_id, now=moment
            )
            return
        if action.kind not in _EXIT_KINDS:
            raise PositionActionUnsupportedError(
                f"{action.kind.value} is decided but this sink cannot carry it out"
            )
        await self._close(
            action, position=position, position_id=position_id, now=moment
        )
        if action.account_halt:
            # After the close, not before. Halting first would leave a
            # position open behind a stopped account, which is the one state
            # the emergency exists to get out of.
            async with self._sessions() as session:
                await trip_kill_switch(
                    session, level=KillSwitchLevel.EMERGENCY, now=moment
                )
                await session.commit()

    async def _close(
        self,
        action: V6PositionAction,
        *,
        position: V6ManagedPosition,
        position_id: UUID,
        now: datetime,
    ) -> None:
        # `or` would read an explicit zero as "unspecified" and close the
        # whole position, which is the opposite of what a zero asks for.
        quantity = (
            position.remaining_quantity if action.quantity is None else action.quantity
        )
        if quantity <= 0:
            return
        async with self._sessions() as session:
            command_id = await self._create_exit_order(
                session,
                action=action,
                position=position,
                position_id=position_id,
                quantity=quantity,
                now=now,
            )
            await session.commit()
        if command_id is None:
            return
        async with self._sessions() as session:
            await DispatchService(
                store=MySqlDispatchStore(session), broker=self._broker
            ).dispatch(command_id=command_id, now=now)
            await session.commit()

    async def _move_stop(
        self,
        action: V6PositionAction,
        *,
        position: V6ManagedPosition,
        position_id: UUID,
        now: datetime,
    ) -> None:
        """Put the stop where the manager says it belongs.

        One path for both actions, and deliberately so. `ACTIVATE_INITIAL_STOP`
        means nothing is working and `MOVE_STOP_TO_BREAK_EVEN` means something
        is, but the manager decides that from what it was told a pass ago. If
        it is wrong, the branch below is what stops a second stop being placed
        behind one position - which is a state nothing downstream would flag,
        because two working stops both look like protection.
        """
        stop_price = action.stop_price
        if stop_price is None or stop_price <= 0:
            raise PositionActionUnsupportedError(
                f"{action.kind.value} names no stop price to move to"
            )
        async with self._sessions() as session:
            working = await self._working_stop(session)
            if working is None:
                command_id = await create_protective_order(
                    session,
                    account=self._account,
                    plan=ProtectionPlan(
                        position_id=position_id,
                        instrument_id=self._instrument_id,
                        entry_side=position.side,
                        quantity=position.remaining_quantity,
                        structural_stop=stop_price,
                    ),
                    now=now,
                )
            else:
                command_id = await self._replace_stop(
                    session, working, stop_price=stop_price, now=now
                )
            await session.commit()
        if command_id is None:
            return
        async with self._sessions() as session:
            await DispatchService(
                store=MySqlDispatchStore(session), broker=self._broker
            ).dispatch(command_id=command_id, now=now)
            await session.commit()

    async def _working_stop(
        self, session: AsyncSession
    ) -> tuple[PersistedOrder, str] | None:
        """The protective order that is actually working, and its broker id.

        A replace has to name what it replaces, and only an order the broker
        acknowledged has an id to name. One that was never dispatched is not
        working, so it is not what a move is about.
        """
        row = (
            await session.execute(
                select(PersistedOrder, PersistedBrokerOrderLink.broker_order_id)
                .join(
                    PersistedOrderIntent,
                    PersistedOrderIntent.id == PersistedOrder.order_intent_id,
                )
                .join(
                    PersistedBrokerOrderLink,
                    PersistedBrokerOrderLink.order_id == PersistedOrder.id,
                )
                .where(
                    PersistedOrder.account_id == self._account.account.id,
                    PersistedOrder.instrument_id == self._instrument_id,
                    PersistedOrderIntent.intent_type == IntentType.PROTECTIVE.value,
                    PersistedOrder.filled_quantity == 0,
                )
                .order_by(PersistedOrder.created_at.desc())
                .limit(1)
                .with_for_update(of=PersistedOrder)
            )
        ).first()
        return None if row is None else (row[0], row[1])

    async def _replace_stop(
        self,
        session: AsyncSession,
        working: tuple[PersistedOrder, str],
        *,
        stop_price: Decimal,
        now: datetime,
    ) -> UUID | None:
        """Amend the working stop rather than cancelling and re-placing it.

        Cancel-then-place leaves a moment with nothing behind the position,
        and that moment is exactly what the stop exists for. A replace is one
        act at the venue and one write in the journal.
        """
        order, broker_order_id = working
        if order.trigger_price == stop_price:
            # Already where it is being moved to. Issuing the replace anyway
            # would spend a command and a version on nothing.
            return None
        order.trigger_price = stop_price
        # The command's canonical payload is built from the order's terms, so
        # the amendment lands first and the version moves with it.
        order.aggregate_version += 1
        await session.flush()
        return await MySqlOrderStore(session).command_for_existing_order(
            order=order,
            command_type=CommandType.REPLACE,
            submission=OrderSubmissionContext(
                broker_client_order_id=order.broker_client_order_id,
                owner_runtime_instance_id=self._account.runtime_instance_id,
                fencing_token=self._account.fencing_token,
                not_after=now + _RESERVATION_WINDOW,
                time_in_force="GTC",
                # Only ever tightens: a replace may not increase exposure.
                authority_class=REPLACE_NON_INCREASING,
                created_at=now,
            ),
            origin=IntentOrigin.STRATEGY,
            target_broker_order_id=broker_order_id,
        )

    async def _add(
        self,
        action: V6PositionAction,
        *,
        position: V6ManagedPosition,
        position_id: UUID,
        now: datetime,
    ) -> None:
        """Move the stop, then add. That order, and it is the whole design.

        The add and the stop move are two acts at the venue and cannot be made
        one, so what matters is which failure they leave behind.

        Stop first: if the add then fails, the position is the size it was,
        behind a stop computed for a larger one. The weighted break-even sits
        between the original entry and the current price, so for the existing
        position that stop is tighter than the one it had - a worse fill if it
        triggers, never more risk.

        Add first would leave the reverse: a position enlarged and still behind
        the old stop, which is more money at risk than anything approved it.
        That is the state this ordering exists to make impossible.

        The quantity is the manager's, and so is the arithmetic behind it -
        `_add_action` only returns one when the resulting worst case is no
        greater than what was already approved, which is why this reserves the
        resulting risk rather than a new allowance.
        """
        if action.stop_price is None or action.stop_price <= 0:
            raise PositionActionUnsupportedError(
                "an add names no stop to move to, and adding without the move "
                "would enlarge the position behind the old stop"
            )
        if action.quantity is None or action.quantity <= 0:
            raise PositionActionUnsupportedError("an add names no quantity")

        await self._move_stop(
            action, position=position, position_id=position_id, now=now
        )

        async with self._sessions() as session:
            command_id = await self._create_add_order(
                session, action, position=position, position_id=position_id, now=now
            )
            await session.commit()
        if command_id is None:
            return
        async with self._sessions() as session:
            await DispatchService(
                store=MySqlDispatchStore(session), broker=self._broker
            ).dispatch(command_id=command_id, now=now)
            await session.commit()

    async def _create_add_order(
        self,
        session: AsyncSession,
        action: V6PositionAction,
        *,
        position: V6ManagedPosition,
        position_id: UUID,
        now: datetime,
    ) -> UUID | None:
        assert action.quantity is not None
        intent = OrderIntentFactory().from_protection(
            account=self._account.account,
            request=ProtectionRequest(
                locked_position_id=position_id,
                reason_code=action.kind.value,
                instrument_id=self._instrument_id,
                # An entry: it opens exposure, which is why it reserves risk
                # and carries new-exposure authority rather than a reduction.
                intent_type=IntentType.ENTRY,
                side=position.side,
                order_style=OrderStyle.MARKET,
                terms=OrderTerms(
                    requested_quantity=action.quantity,
                    limit_price=None,
                    trigger_price=None,
                ),
            ),
        )
        row = _persisted_intent(intent, position_id, now)
        row.strategy_signal_id = None
        row.protection_position_id = position_id
        row.protection_reason_code = action.kind.value
        stored = await OrderIntentRepository(session).create_or_get(row)
        risk_decision = PersistedRiskDecision(
            id=new_uuid7(),
            order_intent_id=stored.id,
            policy_version_id=self._account.policy_version_id,
            risk_snapshot_id=self._account.risk_snapshot_id,
            outcome="APPROVE",
            requested_quantity=action.quantity,
            approved_quantity=action.quantity,
            approved_limit_price=None,
            # What the position will risk once this order and its stop move
            # have both landed. The manager only proposes an add when that is
            # no greater than the risk already approved, so this restates an
            # allowance rather than asking for a new one.
            reserved_risk_amount=action.resulting_worst_case_risk,
            currency=self._account.currency,
            reason_codes=[action.kind.value],
            decision_hash=_digest(f"add:{stored.id.hex}"),
            decided_at=now,
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
        command_id = new_uuid7()
        # Chosen here rather than inside the factory: Binance
        # USD-M requires the client order id to name the command,
        # and recovery reconstructs it from the command alone.
        order = await OrderService(
            store=MySqlOrderStore(session)
        ).create_from_risk_decision(
            decision=_domain_decision(risk_decision, stored.id),
            intent=intent,
            submission=OrderSubmissionContext(
                broker_client_order_id=binance_normal_client_order_id(command_id),
                command_id=command_id,
                owner_runtime_instance_id=self._account.runtime_instance_id,
                fencing_token=self._account.fencing_token,
                not_after=now + _RESERVATION_WINDOW,
                time_in_force="GTC",
                authority_class=NEW_EXPOSURE,
                created_at=now,
            ),
        )
        if order is None:
            return None
        await session.flush()
        return await session.scalar(
            select(PersistedOrderCommand.id).where(
                PersistedOrderCommand.order_id == order.id
            )
        )

    async def _create_exit_order(
        self,
        session: AsyncSession,
        *,
        action: V6PositionAction,
        position: V6ManagedPosition,
        position_id: UUID,
        quantity: Decimal,
        now: datetime,
    ) -> UUID | None:
        intent = OrderIntentFactory().from_protection(
            account=self._account.account,
            request=ProtectionRequest(
                locked_position_id=position_id,
                reason_code=action.kind.value,
                instrument_id=self._instrument_id,
                intent_type=IntentType.EXIT,
                # Closing, so the opposite of what is held.
                side=Side.SELL if position.side is Side.BUY else Side.BUY,
                order_style=OrderStyle.MARKET,
                terms=OrderTerms(
                    requested_quantity=quantity,
                    limit_price=None,
                    trigger_price=None,
                ),
                # Closing at the market, so the same rule as the entry: the
                # price it will get has to be known now.
                quote=await self._quotes.quote(),
            ),
        )
        row = _persisted_intent(intent, position_id, now)
        # Owed to a position rather than to a signal, and the row records
        # which rule closed it so an operator sees why, not only that it did.
        row.strategy_signal_id = None
        row.protection_position_id = position_id
        row.protection_reason_code = action.kind.value
        stored = await OrderIntentRepository(session).create_or_get(row)
        risk_decision = _reduction_decision(
            intent_id=stored.id,
            quantity=quantity,
            account=self._account,
            now=now,
        )
        await RiskReservationService(
            uow=cast(RiskReservationUow, MySqlRiskReservationUow(session))
        ).persist_approval(
            decision=cast(RiskDecisionRecord, risk_decision),
            reservation=cast(
                RiskReservationRecord,
                _consumed_reservation(risk_decision, self._account.account.id, now),
            ),
            account_id=self._account.account.id,
            currency=self._account.currency,
        )
        command_id = new_uuid7()
        # Chosen here rather than inside the factory: Binance
        # USD-M requires the client order id to name the command,
        # and recovery reconstructs it from the command alone.
        order = await OrderService(
            store=MySqlOrderStore(session)
        ).create_from_risk_decision(
            decision=_domain_decision(risk_decision, stored.id),
            intent=intent,
            submission=OrderSubmissionContext(
                broker_client_order_id=binance_normal_client_order_id(command_id),
                command_id=command_id,
                owner_runtime_instance_id=self._account.runtime_instance_id,
                fencing_token=self._account.fencing_token,
                not_after=now + _RESERVATION_WINDOW,
                time_in_force="GTC",
                # Closing only, the same authority the stop carries, because
                # this is the same kind of act.
                authority_class=STRICT_REDUCTION,
                created_at=now,
            ),
        )
        if order is None:
            return None
        await session.flush()
        return await session.scalar(
            select(PersistedOrderCommand.id).where(
                PersistedOrderCommand.order_id == order.id
            )
        )


class MySqlFillSettlement:
    """Resolve paper orders whose fill bar has closed since the last pass."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        bars: ExecutionBars,
        account: ExecutionAccount,
        broker: BrokerSubmitter,
    ) -> None:
        self._sessions = sessions
        self._bars = bars
        self._account = account
        self._broker = broker

    async def settle(self, now: datetime) -> int:
        moment = require_utc(now)
        async with self._sessions() as session:
            journal = MySqlPaperJournal(session)
            receipts = await resolve_paper_fills(
                broker=InternalPaperBroker(
                    journal=journal, market_data=cast(Any, self._bars)
                ),
                journal=journal,
                bars=self._bars,
                now=moment,
            )
            for receipt in receipts:
                await self._apply_to_ledger(session, receipt, moment)
            await session.commit()
        # The protective order is placed against a committed position, and
        # dispatched against a committed command, for the same reason the entry
        # is: the broker is reached from a second connection, which cannot see
        # an open transaction.
        for receipt in receipts:
            await self._protect(receipt, moment)
        return len(receipts)

    async def _protect(self, receipt: PaperOrderReceipt, now: datetime) -> None:
        """Place the stop the decision named, now that the entry has filled.

        Section 9.2 puts the stop outside the confirmed low or high of the leg,
        and the decision recorded that price when it was made. A filled entry
        with no stop behind it is the one state this system must not sit in.
        """
        if receipt.filled_quantity <= 0:
            return
        async with self._sessions() as session:
            plan = await protection_plan(
                session,
                order_id=receipt.order_id,
                filled_quantity=receipt.filled_quantity,
            )
            if plan is None:
                return
            command_id = await self._create_protective_order(session, plan, now)
            await session.commit()
        if command_id is None:
            return
        async with self._sessions() as session:
            await DispatchService(
                store=MySqlDispatchStore(session), broker=self._broker
            ).dispatch(command_id=command_id, now=now)
            await session.commit()

    async def _create_protective_order(
        self, session: AsyncSession, plan: ProtectionPlan, now: datetime
    ) -> UUID | None:
        return await create_protective_order(
            session, account=self._account, plan=plan, now=now
        )

    async def _apply_to_ledger(
        self, session: AsyncSession, receipt: PaperOrderReceipt, now: datetime
    ) -> None:
        """Carry a settled receipt into the position ledger.

        An order that fills and never reaches exec_position leaves the system
        believing it holds nothing, and everything downstream that reads the
        ledger — reconciliation drift, protective-stop enforcement — believes
        it too.
        """
        order = await session.get(PersistedOrder, receipt.order_id)
        if order is None:
            raise LookupError("a settled paper order has no canonical order")
        link = await session.scalar(
            select(PersistedBrokerOrderLink).where(
                PersistedBrokerOrderLink.order_id == order.id,
                PersistedBrokerOrderLink.broker_order_id
                == paper_broker_order_id(receipt.command_id),
            )
        )
        if link is None:
            raise LookupError("a settled paper order has no broker link")
        intent = await session.get(PersistedOrderIntent, order.order_intent_id)
        if intent is None:
            raise LookupError("a settled paper order has no intent")
        event = paper_execution_event(
            receipt=receipt,
            account_id=order.account_id,
            instrument_id=order.instrument_id,
            broker_id=link.broker_id,
            broker_client_order_id=order.broker_client_order_id,
            side=Side(order.side),
            currency=self._account.currency,
            leg_role=LEG_ROLES[IntentType(intent.intent_type)],
            observed_at=now,
        )
        if event is None:
            # A no-fill moved nothing, so there is no execution to record.
            return
        await MySqlFillStore(session).apply_event_once(event)
