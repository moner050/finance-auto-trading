from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from autotrader.integrations.brokers.common import BrokerWriteDisabled
from autotrader.integrations.brokers.toss.submit_recovery import (
    TossPostSendFailure,
    TossPostSendFailureKind,
    TossRecoveryState,
)
from autotrader.integrations.brokers.toss.us_cash_writer import (
    TossUsCashWriterUnknown,
)
from autotrader.shared.ids import new_uuid7
from tests.unit.integrations.brokers.toss.test_toss_us_cash_writer import (
    Clock,
    MemoryRecoveryStore,
    Sender,
    command,
    context,
    success,
    writer,
)


@pytest.mark.asyncio
async def test_identical_recovery_replays_once_at_599_seconds() -> None:
    value = command()
    write_context = context(value)
    store = MemoryRecoveryStore()
    first_sender = Sender(
        store,
        [TossPostSendFailure(TossPostSendFailureKind.TIMEOUT)],
    )
    with pytest.raises(TossUsCashWriterUnknown):
        await writer(value, write_context, store, first_sender).submit_locked(value)

    clock = Clock(value.dispatch_attempted_at + timedelta(seconds=599))  # type: ignore[operator]
    recovery_sender = Sender(store, [success(value)])
    recovered = await writer(
        value,
        write_context,
        store,
        recovery_sender,
        clock=clock,
    ).recover(value.id)

    assert recovered.provider_state == "RECOVERED"
    assert len(recovery_sender.calls) == 1
    assert store.rows[value.id].replay_count == 1
    assert store.rows[value.id].state is TossRecoveryState.ACKNOWLEDGED


@pytest.mark.asyncio
async def test_recovery_refuses_the_exclusive_600_second_boundary() -> None:
    value = command()
    write_context = context(value)
    store = MemoryRecoveryStore()
    first_sender = Sender(store, [TossPostSendFailure(TossPostSendFailureKind.RESET)])
    with pytest.raises(TossUsCashWriterUnknown):
        await writer(value, write_context, store, first_sender).submit_locked(value)

    clock = Clock(value.dispatch_attempted_at + timedelta(seconds=600))  # type: ignore[operator]
    recovery_sender = Sender(store, [success(value)])
    with pytest.raises(TossUsCashWriterUnknown, match="terminal"):
        await writer(
            value,
            write_context,
            store,
            recovery_sender,
            clock=clock,
        ).recover(value.id)

    assert recovery_sender.calls == []
    assert store.rows[value.id].state is TossRecoveryState.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("body", "header"))
async def test_changed_body_or_nonvolatile_header_cannot_recover(change: str) -> None:
    value = command()
    original = context(value)
    store = MemoryRecoveryStore()
    first_sender = Sender(store, [TossPostSendFailure(TossPostSendFailureKind.TIMEOUT)])
    with pytest.raises(TossUsCashWriterUnknown):
        await writer(value, original, store, first_sender).submit_locked(value)
    changed = replace(
        original,
        symbol="MSFT" if change == "body" else original.symbol,
        account_seq=18 if change == "header" else original.account_seq,
    )
    recovery_sender = Sender(store, [success(value)])

    with pytest.raises(BrokerWriteDisabled, match="digest"):
        await writer(value, changed, store, recovery_sender).recover(value.id)

    assert recovery_sender.calls == []
    assert store.rows[value.id].replay_count == 0


@pytest.mark.asyncio
async def test_concurrent_recovery_has_one_exclusive_sender() -> None:
    value = command()
    write_context = context(value)
    store = MemoryRecoveryStore()
    first_sender = Sender(store, [TossPostSendFailure(TossPostSendFailureKind.TIMEOUT)])
    with pytest.raises(TossUsCashWriterUnknown):
        await writer(value, write_context, store, first_sender).submit_locked(value)

    first_recovery_sender = Sender(store, [success(value)])
    second_recovery_sender = Sender(store, [success(value)])
    first = writer(
        value,
        write_context,
        store,
        first_recovery_sender,
        clock=Clock(value.dispatch_attempted_at + timedelta(seconds=1)),  # type: ignore[operator]
        lease_owner=new_uuid7(),
    )
    second = writer(
        value,
        write_context,
        store,
        second_recovery_sender,
        clock=Clock(value.dispatch_attempted_at + timedelta(seconds=1)),  # type: ignore[operator]
        lease_owner=new_uuid7(),
    )

    outcomes = await asyncio.gather(
        first.recover(value.id), second.recover(value.id), return_exceptions=True
    )

    assert len(first_recovery_sender.calls) + len(second_recovery_sender.calls) == 1
    assert any(not isinstance(outcome, BaseException) for outcome in outcomes)
    assert store.rows[value.id].state is TossRecoveryState.ACKNOWLEDGED


@pytest.mark.asyncio
async def test_one_failed_recovery_becomes_unknown_and_never_reposts() -> None:
    value = command()
    write_context = context(value)
    store = MemoryRecoveryStore()
    first_sender = Sender(store, [TossPostSendFailure(TossPostSendFailureKind.TIMEOUT)])
    with pytest.raises(TossUsCashWriterUnknown):
        await writer(value, write_context, store, first_sender).submit_locked(value)

    failed_sender = Sender(store, [TossPostSendFailure(TossPostSendFailureKind.RESET)])
    with pytest.raises(TossUsCashWriterUnknown):
        await writer(
            value,
            write_context,
            store,
            failed_sender,
            clock=Clock(value.dispatch_attempted_at + timedelta(seconds=1)),  # type: ignore[operator]
        ).recover(value.id)

    restarted_sender = Sender(store, [success(value)])
    with pytest.raises(TossUsCashWriterUnknown, match="terminal"):
        await writer(
            value,
            write_context,
            store,
            restarted_sender,
            clock=Clock(value.dispatch_attempted_at + timedelta(seconds=2)),  # type: ignore[operator]
        ).recover(value.id)

    assert restarted_sender.calls == []
    assert store.rows[value.id].state is TossRecoveryState.UNKNOWN
