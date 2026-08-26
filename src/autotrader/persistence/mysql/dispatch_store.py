"""Durable record of what was sent to a broker.

The dispatch service writes an irreversible marker before it crosses the
broker boundary, so that a crash between the send and the reply can never be
mistaken for "never sent". This store keeps that marker and the terminal state
that follows it, and nothing else: authority was already decided when the
command was created.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.persistence.mysql.models.accounts import Account
from autotrader.persistence.mysql.models.orders import (
    PersistedBrokerOrderLink,
    PersistedOrderCommand,
)
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
UNKNOWN = "UNKNOWN"
# Once a broker has accepted, nothing may overwrite that fact.
_TERMINAL = (ACCEPTED,)


class MySqlDispatchStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def authorize_and_record_attempt(
        self, *, command_id: UUID, now: datetime
    ) -> BrokerOrderCommand | None:
        """Claim a command exactly once and mark the attempt before sending."""
        moment = require_utc(now)
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
        time_in_force=row.time_in_force,
        status=row.status,
        dispatch_attempted_at=(
            require_utc(row.dispatch_attempted_at)
            if row.dispatch_attempted_at is not None
            else None
        ),
    )


__all__ = ("ACCEPTED", "REJECTED", "UNKNOWN", "MySqlDispatchStore")
