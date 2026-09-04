"""Durable record of what was sent to a broker.

The dispatch service writes an irreversible marker before it crosses the
broker boundary, so that a crash between the send and the reply can never be
mistaken for "never sent". This store keeps that marker and the terminal state
that follows it, and nothing else: authority was already decided when the
command was created.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.config.settings import RuntimeMode
from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.controls.gates import SubmissionGate
from autotrader.execution.controls.models import (
    ArmLease,
    ExposureEffect,
    GateAction,
    SubmissionContext,
)
from autotrader.execution.dispatch.authorization import (
    DispatchAuthorizationState,
    decide_dispatch,
)
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.common import CLOSING_AUTHORITY
from autotrader.persistence.mysql.models.accounts import Account
from autotrader.persistence.mysql.models.operations import (
    OpsIncident,
    OpsSchedulerLease,
    OpsTradingControl,
)
from autotrader.persistence.mysql.models.orders import (
    PersistedBrokerOrderLink,
    PersistedOrderCommand,
)
from autotrader.persistence.mysql.models.reconciliation import (
    PersistedReconciliationDiff,
)
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
UNKNOWN = "UNKNOWN"
NO_KILL_SWITCH = "NONE"
_ACTIONS = {
    CommandType.SUBMIT: GateAction.SUBMIT,
    CommandType.CANCEL: GateAction.CANCEL,
    CommandType.REPLACE: GateAction.REPLACE,
}


def _arm_lease(control: OpsTradingControl) -> ArmLease | None:
    """The arming, as the gate reads it, or nothing when it is not held."""
    if (
        control.owner_runtime_instance_id is None
        or control.acquired_at is None
        or control.expires_at is None
    ):
        return None
    return ArmLease(
        owner_runtime_instance_id=control.owner_runtime_instance_id,
        acquired_at=require_utc(control.acquired_at),
        expires_at=require_utc(control.expires_at),
        fencing_token=control.fencing_token,
        row_version=control.row_version,
    )


# Once a broker has accepted, nothing may overwrite that fact.
_TERMINAL = (ACCEPTED,)


@dataclass(frozen=True, slots=True)
class RuntimeFacts:
    """What the gates need and no table holds.

    Required rather than defaulted, all of them. A permissive default here
    is a live write nobody asked for, and the two facts that decide it -
    `allow_live` and the runtime mode - are the ones a caller is most likely
    to leave off.

    `market_data_fresh` is a fact about the moment, not the process, so it is
    a callable. A store that could only say "yes, when I was built" would be
    answering about a different moment than the one it is asked in.
    """

    runtime_mode: RuntimeMode
    allow_live: bool
    account_environment: RuntimeMode
    local_runtime_instance_id: UUID
    market_data_fresh: Callable[[], bool]


class MySqlDispatchStore:
    def __init__(
        self, session: AsyncSession, facts: RuntimeFacts | None = None
    ) -> None:
        self._session = session
        # Absent means no venue write can be authorised through this store.
        # Reading and recovery need no runtime facts; sending does, and a
        # store built without them refuses rather than assuming any.
        self._facts = facts

    async def authorize_and_record_attempt(
        self, *, command_id: UUID, now: datetime
    ) -> BrokerOrderCommand | None:
        """Claim a command exactly once, and only if it may still be sent.

        `decide_dispatch` runs here because this is the one place a command is
        claimed before crossing the broker boundary, so every dispatch path
        meets it. It had no callers at all: the fencing token, the arming
        lease and the trading control were checked by nothing that ran, and a
        process that had lost its lease could still have written. §31.12.

        The gate runs against every control row and all of them must allow it.
        One row per scope, and an account in trouble at any scope is in
        trouble - the money is the same money.
        """
        moment = require_utc(now)
        if not await self._may_dispatch(command_id, moment):
            return None
        if not await self._may_submit(command_id, moment):
            return None
        claimed = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(PersistedOrderCommand)
                .where(
                    PersistedOrderCommand.id == command_id,
                    PersistedOrderCommand.dispatch_attempted_at.is_(None),
                    PersistedOrderCommand.result_state.is_(None),
                    PersistedOrderCommand.not_after > moment,
                )
                .values(dispatch_attempted_at=moment)
            ),
        )
        if claimed.rowcount != 1:
            return None
        await self._session.flush()
        return await self._command(command_id)

    async def _may_dispatch(self, command_id: UUID, now: datetime) -> bool:
        row = await self._row(command_id)
        if row is None:
            return False
        lease = (await self._session.scalars(select(OpsSchedulerLease))).all()
        controls = (await self._session.scalars(select(OpsTradingControl))).all()
        if not controls:
            # Nobody armed anything, which is not armed. The gate says so too,
            # but with no rows there is nothing to run it against.
            return False
        blocking = await self._blocking_incidents()
        unknown = await self._unresolved_unknowns()
        # A closing order and a cancel are allowed past the arming checks by
        # the gate itself: refusing to reduce exposure is not a safe default.
        closing = row.authority_class == CLOSING_AUTHORITY
        cancelling = row.command_type == CommandType.CANCEL.value
        owned = [item for item in lease if item.owner_runtime_instance_id is not None]
        held = owned[0] if len(owned) == 1 else None
        for control in controls:
            decision = decide_dispatch(
                DispatchAuthorizationState(
                    now=now,
                    not_after=require_utc(row.not_after),
                    attempt_recorded=row.dispatch_attempted_at is not None,
                    command_owner=row.owner_runtime_instance_id,
                    command_fencing_token=row.fencing_token,
                    lease_owner=None
                    if held is None
                    else held.owner_runtime_instance_id,
                    lease_fencing_token=None if held is None else held.fencing_token,
                    lease_expires_at=(
                        None
                        if held is None or held.expires_at is None
                        else require_utc(held.expires_at)
                    ),
                    control_owner=control.owner_runtime_instance_id,
                    control_fencing_token=control.fencing_token,
                    control_expires_at=(
                        None
                        if control.expires_at is None
                        else require_utc(control.expires_at)
                    ),
                    control_armed=control.armed,
                    kill_switch_active=control.kill_switch_level != NO_KILL_SWITCH,
                    blocking_incident_count=blocking,
                    unresolved_unknown_count=unknown,
                    strict_reduction_proven=closing,
                    cancel_authorized=cancelling,
                )
            )
            if not decision.allowed:
                return False
        return True

    async def _may_submit(self, command_id: UUID, now: datetime) -> bool:
        """The runtime half: the mode, the venue permission, the environment.

        `SubmissionGate` was used only by its own tests, so `allow_live` - the
        switch between a LIVE build existing and LIVE being able to trade -
        was checked by nothing that ran. §31.12.
        """
        facts = self._facts
        if facts is None:
            return False
        row = await self._row(command_id)
        if row is None:
            return False
        controls = (await self._session.scalars(select(OpsTradingControl))).all()
        if not controls:
            return False
        blocking = await self._blocking_incidents()
        unknown = await self._unresolved_unknowns()
        reconciling = await self._blocking_reconciliations()
        fresh = facts.market_data_fresh()
        for control in controls:
            decision = SubmissionGate().evaluate(
                SubmissionContext(
                    now=now,
                    action=_ACTIONS[CommandType(row.command_type)],
                    runtime_mode=facts.runtime_mode,
                    allow_live=facts.allow_live,
                    account_environment=facts.account_environment.value,
                    local_runtime_instance_id=facts.local_runtime_instance_id,
                    locally_armed=control.armed,
                    arm_lease=_arm_lease(control),
                    # The write about to happen is in this session, so the
                    # database is writable by the only test that matters.
                    database_writable=True,
                    market_data_fresh=fresh,
                    active_kill_switch=control.kill_switch_level != NO_KILL_SWITCH,
                    blocking_incident_count=blocking,
                    unresolved_unknown_count=unknown,
                    blocking_reconciliation_count=reconciling,
                    exposure_effect=(
                        ExposureEffect.REDUCE
                        if row.authority_class == CLOSING_AUTHORITY
                        else ExposureEffect.INCREASE
                    ),
                )
            )
            if not decision.allowed:
                return False
        return True

    async def _blocking_reconciliations(self) -> int:
        return (
            await self._session.scalar(
                select(func.count())
                .select_from(PersistedReconciliationDiff)
                .where(
                    PersistedReconciliationDiff.severity == "BLOCKING",
                    PersistedReconciliationDiff.status == "OPEN",
                )
            )
        ) or 0

    async def _blocking_incidents(self) -> int:
        return (
            await self._session.scalar(
                select(func.count())
                .select_from(OpsIncident)
                .where(
                    OpsIncident.severity == "BLOCKING",
                    OpsIncident.status == "OPEN",
                )
            )
        ) or 0

    async def _unresolved_unknowns(self) -> int:
        """Commands the broker may hold and nobody has resolved.

        Sending a new one while an old one is unresolved risks two live orders
        for one decision, which is what the count is here to stop.
        """
        return (
            await self._session.scalar(
                select(func.count())
                .select_from(PersistedOrderCommand)
                .where(PersistedOrderCommand.result_state == UNKNOWN)
            )
        ) or 0

    async def recoverable_command(
        self, *, command_id: UUID
    ) -> BrokerOrderCommand | None:
        """A command already sent whose outcome is still unresolved."""
        row = await self._row(command_id)
        if row is None or row.dispatch_attempted_at is None:
            return None
        if row.result_state in _TERMINAL or row.result_state == REJECTED:
            return None
        return _to_command(row)

    async def command_for_recovery(self, *, command_id: UUID) -> BrokerOrderCommand:
        command = await self._command(command_id)
        if command is None:
            raise ValueError("dispatch recovery requires an existing command")
        return command

    async def record_recovery_attempt(self, *, command_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(PersistedOrderCommand)
            .where(
                PersistedOrderCommand.id == command_id,
                PersistedOrderCommand.result_state.not_in(_TERMINAL)
                | PersistedOrderCommand.result_state.is_(None),
            )
            .values(dispatch_attempted_at=require_utc(now))
        )
        await self._session.flush()

    async def record_accepted(
        self, *, command_id: UUID, broker_order_id: str, now: datetime
    ) -> None:
        if not broker_order_id or broker_order_id.strip() != broker_order_id:
            raise ValueError("broker_order_id must be non-empty trimmed text")
        moment = require_utc(now)
        row = await self._row(command_id)
        if row is None:
            raise ValueError("dispatch result requires an existing command")
        if row.result_state == ACCEPTED:
            return
        row.result_state = ACCEPTED
        row.dispatch_attempted_at = row.dispatch_attempted_at or moment
        await self._link_broker_order(row, broker_order_id)
        await self._session.flush()

    async def record_rejected(self, *, command_id: UUID, now: datetime) -> None:
        await self._record_terminal(command_id, REJECTED, now)

    async def record_unknown(
        self, *, command_id: UUID, now: datetime, deadline: datetime
    ) -> None:
        require_utc(deadline)
        await self._record_terminal(command_id, UNKNOWN, now)

    async def _record_terminal(
        self, command_id: UUID, state: str, now: datetime
    ) -> None:
        moment = require_utc(now)
        row = await self._row(command_id)
        if row is None:
            raise ValueError("dispatch result requires an existing command")
        # An accepted send is the ground truth and outranks a later report.
        if row.result_state in _TERMINAL or row.result_state == state:
            return
        row.result_state = state
        row.dispatch_attempted_at = row.dispatch_attempted_at or moment
        await self._session.flush()

    async def _link_broker_order(
        self, row: PersistedOrderCommand, broker_order_id: str
    ) -> None:
        # The broker a command reaches is the one its account is bound to.
        account = await self._session.get(Account, row.account_id)
        if account is None:
            raise ValueError("dispatch result requires an existing account")
        broker_id = account.broker_id
        existing = await self._session.scalar(
            select(PersistedBrokerOrderLink).where(
                PersistedBrokerOrderLink.order_id == row.order_id,
                PersistedBrokerOrderLink.broker_id == broker_id,
                PersistedBrokerOrderLink.broker_order_id == broker_order_id,
            )
        )
        if existing is not None:
            return
        highest = await self._session.scalar(
            select(PersistedBrokerOrderLink.link_sequence)
            .where(PersistedBrokerOrderLink.order_id == row.order_id)
            .order_by(PersistedBrokerOrderLink.link_sequence.desc())
            .limit(1)
        )
        self._session.add(
            PersistedBrokerOrderLink(
                id=new_uuid7(),
                order_id=row.order_id,
                broker_id=broker_id,
                broker_order_id=broker_order_id,
                link_sequence=(highest or 0) + 1,
                exposure_bearing=row.command_type == CommandType.SUBMIT.value,
                status="ACTIVE",
            )
        )

    async def _row(self, command_id: UUID) -> PersistedOrderCommand | None:
        return await self._session.get(PersistedOrderCommand, command_id)

    async def _command(self, command_id: UUID) -> BrokerOrderCommand | None:
        row = await self._row(command_id)
        return None if row is None else _to_command(row)


def _to_command(row: PersistedOrderCommand) -> BrokerOrderCommand:
    return BrokerOrderCommand(
        id=row.id,
        order_id=row.order_id,
        account_id=row.account_id,
        instrument_id=row.instrument_id,
        command_type=CommandType(row.command_type),
        target_aggregate_version=row.target_aggregate_version,
        idempotency_key=row.idempotency_key,
        command_sequence=row.command_sequence,
        canonical_payload_hash=row.canonical_payload_hash,
        broker_client_order_id=row.broker_client_order_id,
        target_broker_order_id=row.target_broker_order_id,
        replaces_command_id=row.replaces_command_id,
        origin_type=row.origin_type,
        authority_class=row.authority_class,
        owner_runtime_instance_id=row.owner_runtime_instance_id,
        fencing_token=row.fencing_token,
        not_after=require_utc(row.not_after),
        side=Side(row.side),
        order_style=OrderStyle(row.order_style),
        quantity=row.quantity,
        limit_price=row.limit_price,
        trigger_price=row.trigger_price,
        time_in_force=row.time_in_force,
        status=row.status,
        dispatch_attempted_at=(
            require_utc(row.dispatch_attempted_at)
            if row.dispatch_attempted_at is not None
            else None
        ),
    )


__all__ = ("ACCEPTED", "REJECTED", "UNKNOWN", "MySqlDispatchStore")
