from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from typing import Protocol, TypeIs
from uuid import UUID

from autotrader.domain.broker_errors import BrokerSubmissionRejected
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType


class BrokerSubmission(Protocol):
    @property
    def broker_order_id(self) -> str:
        """Read only, so a broker may answer with an immutable receipt."""
        ...


class BrokerSubmitter(Protocol):
    async def submit(self, command: BrokerOrderCommand) -> BrokerSubmission: ...

    async def cancel(self, command: BrokerOrderCommand) -> BrokerSubmission: ...

    async def replace(self, command: BrokerOrderCommand) -> BrokerSubmission: ...

    async def recover_submit(
        self, command: BrokerOrderCommand, *, now: datetime
    ) -> BrokerSubmission | None: ...


class DispatchStore(Protocol):
    async def authorize_and_record_attempt(
        self, *, command_id: UUID, now: datetime
    ) -> BrokerOrderCommand | None: ...

    async def record_unknown(
        self, *, command_id: UUID, now: datetime, deadline: datetime
    ) -> None:
        """Persist UNKNOWN idempotently without downgrading ACCEPTED.

        Implementations must be cancellation-cooperative and stop work when cancelled.
        Ignoring cancellation violates this dispatch-store protocol.
        """
        ...

    async def record_accepted(
        self, *, command_id: UUID, broker_order_id: str, now: datetime
    ) -> None: ...

    async def record_rejected(self, *, command_id: UUID, now: datetime) -> None: ...

    async def record_recovery_attempt(
        self, *, command_id: UUID, now: datetime
    ) -> None: ...

    async def command_for_recovery(self, *, command_id: UUID) -> BrokerOrderCommand: ...

    async def recoverable_command(
        self, *, command_id: UUID
    ) -> BrokerOrderCommand | None: ...


class DispatchService:
    """Records the irreversible dispatch marker before crossing the broker boundary."""

    def __init__(self, *, store: DispatchStore, broker: BrokerSubmitter) -> None:
        self._store = store
        self._broker = broker

    async def dispatch(self, *, command_id: UUID, now: datetime) -> None:
        command = await self._store.authorize_and_record_attempt(
            command_id=command_id, now=now
        )
        if command is None:
            recovery = await self._store.recoverable_command(command_id=command_id)
            if recovery is not None:
                if recovery.command_type is CommandType.SUBMIT:
                    await self._recover(command=recovery, now=now)
                else:
                    await self._store.record_unknown(
                        command_id=recovery.id,
                        now=now,
                        deadline=recovery.not_after,
                    )
            return
        unknown_deadline = asyncio.get_running_loop().time() + max(
            0.0, (command.not_after - now).total_seconds()
        )
        control_error: BaseException | None = None
        try:
            result = await self._dispatch_to_broker(command)
            await self._store.record_accepted(
                command_id=command_id,
                broker_order_id=result.broker_order_id,
                now=now,
            )
        except BrokerSubmissionRejected:
            await self._store.record_rejected(command_id=command_id, now=now)
            return
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as caught:
            _scrub_control_exception(caught)
            control_error = caught
            del caught
        except Exception:
            await self._store.record_unknown(
                command_id=command_id,
                now=now,
                deadline=command.not_after,
            )
            return
        if control_error is not None:
            with suppress(
                asyncio.CancelledError, KeyboardInterrupt, SystemExit, Exception
            ):
                await self._record_unknown_cancellation_safe(
                    command_id=command_id,
                    now=now,
                    deadline=command.not_after,
                    loop_deadline=unknown_deadline,
                )
            error = control_error
            control_error = None
            _scrub_control_exception(error)
            raise error from None

    async def _record_unknown_cancellation_safe(
        self,
        *,
        command_id: UUID,
        now: datetime,
        deadline: datetime,
        loop_deadline: float,
    ) -> None:
        persistence = asyncio.create_task(
            self._record_unknown_safely(
                command_id=command_id,
                now=now,
                deadline=deadline,
            )
        )
        while not persistence.done():
            remaining = loop_deadline - asyncio.get_running_loop().time()
            if remaining <= 0.0:
                break
            try:
                await asyncio.wait((persistence,), timeout=remaining)
            except asyncio.CancelledError:
                continue
        if persistence.done():
            persistence.result()
            return
        persistence.cancel()
        while not persistence.done():
            try:
                await asyncio.shield(persistence)
            except asyncio.CancelledError:
                continue
        persistence.result()

    async def _record_unknown_safely(
        self, *, command_id: UUID, now: datetime, deadline: datetime
    ) -> bool:
        try:
            await self._store.record_unknown(
                command_id=command_id,
                now=now,
                deadline=deadline,
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as caught:
            _scrub_control_exception(caught)
            return False
        except Exception:
            return False
        return True

    async def _dispatch_to_broker(
        self, command: BrokerOrderCommand
    ) -> BrokerSubmission:
        if command.command_type is CommandType.SUBMIT:
            return await self._broker.submit(command)
        if command.command_type is CommandType.CANCEL:
            return await self._broker.cancel(command)
        if command.command_type is CommandType.REPLACE:
            return await self._broker.replace(command)
        raise ValueError("unsupported broker command type")

    async def recover(self, *, command_id: UUID, now: datetime) -> None:
        command = await self._store.command_for_recovery(command_id=command_id)
        await self._recover(command=command, now=now)

    async def _recover(self, *, command: BrokerOrderCommand, now: datetime) -> None:
        if command.command_type is not CommandType.SUBMIT:
            await self._store.record_unknown(
                command_id=command.id,
                now=now,
                deadline=command.not_after,
            )
            return
        attempted_at = command.dispatch_attempted_at
        if (
            not _is_exact_utc(now)
            or not _is_exact_utc(command.not_after)
            or not _is_exact_utc(attempted_at)
            or attempted_at > now
            or now >= command.not_after
        ):
            return
        await self._store.record_recovery_attempt(command_id=command.id, now=now)
        result = await self._broker.recover_submit(command, now=now)
        if result is None:
            await self._store.record_unknown(
                command_id=command.id,
                now=now,
                deadline=command.not_after,
            )
            return
        await self._store.record_accepted(
            command_id=command.id,
            broker_order_id=result.broker_order_id,
            now=now,
        )


def _is_exact_utc(value: object) -> TypeIs[datetime]:
    return (
        type(value) is datetime
        and value.tzinfo is UTC
        and value.utcoffset() == UTC.utcoffset(value)
    )


def _scrub_control_exception(caught: BaseException) -> None:
    caught.__traceback__ = None
    caught.__context__ = None
    caught.__cause__ = None
    caught.args = ()
    caught.__dict__.clear()
    if isinstance(caught, SystemExit):
        caught.code = 1
