from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.common import (
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)
from autotrader.integrations.brokers.toss.submit_recovery import (
    TossPostSendFailure,
    TossPostSendFailureKind,
    TossPreSendFailure,
    TossRecoveryClaim,
    TossRecoveryRecord,
    TossRecoveryState,
    canonical_toss_request_digest,
)
from autotrader.integrations.brokers.toss.us_cash_writer import (
    TossUsCashRecoveryContext,
    TossUsCashWriteContext,
    TossUsCashWriter,
    TossUsCashWriterNotSent,
    TossUsCashWriterUnknown,
)
from autotrader.shared.ids import new_uuid7

NOW = datetime(2026, 8, 24, 1, 0, 0, tzinfo=UTC)


def command() -> BrokerOrderCommand:
    return BrokerOrderCommand(
        id=new_uuid7(),
        order_id=new_uuid7(),
        account_id=new_uuid7(),
        instrument_id=new_uuid7(),
        command_type=CommandType.SUBMIT,
        target_aggregate_version=1,
        idempotency_key="v6-toss-us-submit",
        command_sequence=1,
        canonical_payload_hash=b"c" * 32,
        broker_client_order_id=f"t6-{new_uuid7().hex}",
        target_broker_order_id=None,
        replaces_command_id=None,
        origin_type="STRATEGY",
        authority_class="SUBMIT_NEW_EXPOSURE",
        owner_runtime_instance_id=new_uuid7(),
        fencing_token=7,
        not_after=NOW + timedelta(minutes=15),
        side=Side.BUY,
        order_style=OrderStyle.LIMIT,
        quantity=Decimal("2"),
        limit_price=Decimal("250.25"),
        time_in_force="DAY",
        dispatch_attempted_at=NOW,
    )


def context(
    value: BrokerOrderCommand,
    *,
    symbol: str = "AAPL",
    account_seq: int = 17,
    writer_capability: bool = True,
) -> TossUsCashWriteContext:
    return TossUsCashWriteContext(
        command_id=value.id,
        instrument_id=value.instrument_id,
        account_id=value.account_id,
        provider_binding_id=new_uuid7(),
        binding_generation=7,
        policy_version_id=new_uuid7(),
        strategy_version="david-trullas-v6.0",
        writer_capability=writer_capability,
        account_enabled=False,
        binding_active=True,
        account_seq=account_seq,
        symbol=symbol,
        intent_locked=True,
        opens_exposure=True,
        authorized_sellable_quantity=Decimal("0"),
    )


class Authority:
    def __init__(
        self, value: BrokerOrderCommand, write_context: TossUsCashWriteContext
    ) -> None:
        self.command = value
        self.context = write_context

    async def load(self, command: BrokerOrderCommand) -> TossUsCashWriteContext:
        del command
        return self.context

    async def load_recovery(self, dispatch_id: UUID) -> TossUsCashRecoveryContext:
        del dispatch_id
        return TossUsCashRecoveryContext(self.command, self.context)


class Tokens:
    def __init__(self, token: str = "private-token") -> None:
        self.token = token

    async def load(self, binding_id: UUID) -> str:
        del binding_id
        return self.token


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class MemoryRecoveryStore:
    def __init__(self) -> None:
        self.rows: dict[UUID, TossRecoveryRecord] = {}
        self.lock = asyncio.Lock()
        self.persisted_before_send = False

    async def prepare(self, record: TossRecoveryRecord) -> TossRecoveryClaim:
        async with self.lock:
            current = self.rows.get(record.dispatch_id)
            if current is None:
                self.rows[record.dispatch_id] = record
                return TossRecoveryClaim(record, acquired=True)
            if (
                current.binding_id != record.binding_id
                or current.account_id != record.account_id
                or current.client_order_id != record.client_order_id
                or current.request_digest != record.request_digest
                or current.first_dispatch_at != record.first_dispatch_at
            ):
                raise ValueError("Toss recovery evidence mismatch")
            return TossRecoveryClaim(current, acquired=False)

    async def load(self, dispatch_id: UUID) -> TossRecoveryRecord | None:
        async with self.lock:
            return self.rows.get(dispatch_id)

    async def claim_replay(
        self,
        dispatch_id: UUID,
        *,
        lease_owner: UUID,
        now: datetime,
        request_digest: bytes,
    ) -> TossRecoveryClaim:
        async with self.lock:
            current = self.rows[dispatch_id]
            if current.request_digest != request_digest:
                raise ValueError("Toss recovery request digest mismatch")
            if current.state is not TossRecoveryState.OPEN:
                return TossRecoveryClaim(current, acquired=False)
            if now >= current.lease_expires_at:
                expired = replace(
                    current,
                    state=TossRecoveryState.UNKNOWN,
                    lease_owner=lease_owner,
                    terminal_at=now,
                )
                self.rows[dispatch_id] = expired
                return TossRecoveryClaim(expired, acquired=False)
            if current.replay_count >= 1:
                return TossRecoveryClaim(current, acquired=False)
            claimed = replace(
                current,
                lease_owner=lease_owner,
                lease_acquired_at=now,
                replay_count=1,
            )
            self.rows[dispatch_id] = claimed
            return TossRecoveryClaim(claimed, acquired=True)

    async def finish(
        self,
        dispatch_id: UUID,
        *,
        lease_owner: UUID,
        state: TossRecoveryState,
        terminal_at: datetime,
        provider_order_id: str | None,
    ) -> TossRecoveryRecord:
        async with self.lock:
            current = self.rows[dispatch_id]
            if current.lease_owner != lease_owner:
                raise RuntimeError("stale Toss recovery lease owner")
            completed = replace(
                current,
                state=state,
                terminal_at=terminal_at,
                provider_order_id=provider_order_id,
            )
            completed.validate()
            self.rows[dispatch_id] = completed
            return completed


@dataclass
class Sender:
    store: MemoryRecoveryStore
    outcomes: list[BrokerResponse | BaseException]
    calls: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.store.persisted_before_send = bool(self.store.rows)
        self.calls.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def success(
    value: BrokerOrderCommand,
    order_id: str = "opaque-order-1",
) -> BrokerResponse:
    return BrokerResponse(
        status=200,
        body=json.dumps(
            {
                "result": {
                    "orderId": order_id,
                    "clientOrderId": value.broker_client_order_id,
                }
            }
        ).encode(),
    )


def writer(
    value: BrokerOrderCommand,
    write_context: TossUsCashWriteContext,
    store: MemoryRecoveryStore,
    sender: Sender,
    *,
    clock: Clock | None = None,
    lease_owner: UUID | None = None,
    tokens: Tokens | None = None,
) -> TossUsCashWriter:
    return TossUsCashWriter(
        authority=Authority(value, write_context),
        token_source=Tokens() if tokens is None else tokens,
        store=store,
        sender=sender,
        clock=Clock() if clock is None else clock,
        lease_owner=new_uuid7() if lease_owner is None else lease_owner,
    )


@pytest.mark.asyncio
async def test_first_submit_persists_exact_us_payload_before_network() -> None:
    value = command()
    write_context = context(value)
    store = MemoryRecoveryStore()
    sender = Sender(store, [success(value)])

    result = await writer(value, write_context, store, sender).submit_locked(value)

    assert result.broker_order_id == "TOSS-US:opaque-order-1"
    assert result.provider_state == "ACKNOWLEDGED"
    assert store.persisted_before_send is True
    assert len(sender.calls) == 1
    request = sender.calls[0]
    assert request.method == "POST"
    assert request.path == "/api/v1/orders"
    assert dict(request.headers)["X-Tossinvest-Account"] == "17"
    assert json.loads(request.body or b"") == {
        "clientOrderId": value.broker_client_order_id,
        "confirmHighValueOrder": False,
        "orderType": "LIMIT",
        "price": "250.25",
        "quantity": "2",
        "side": "BUY",
        "symbol": "AAPL",
        "timeInForce": "DAY",
    }
    assert store.rows[value.id].request_digest == canonical_toss_request_digest(request)
    assert store.rows[value.id].state is TossRecoveryState.ACKNOWLEDGED


@pytest.mark.asyncio
async def test_restart_reuses_deterministic_client_id_without_second_post() -> None:
    value = command()
    write_context = context(value)
    store = MemoryRecoveryStore()
    first_sender = Sender(store, [success(value)])
    accepted = await writer(
        value,
        write_context,
        store,
        first_sender,
    ).submit_locked(value)
    restarted_sender = Sender(store, [success(value, "must-not-be-sent")])

    replayed = await writer(
        value,
        write_context,
        store,
        restarted_sender,
        tokens=Tokens("rotated-private-token"),
    ).submit_locked(value)

    assert replayed.broker_order_id == accepted.broker_order_id
    assert restarted_sender.calls == []
    assert store.rows[value.id].client_order_id == value.broker_client_order_id


def test_canonical_request_digest_is_stable_and_excludes_only_authorization() -> None:
    body = b'{"clientOrderId":"fixed","symbol":"AAPL"}'
    first = BrokerRequest(
        method="POST",
        path="/api/v1/orders",
        headers=(
            ("Authorization", "Bearer first"),
            ("Content-Type", "application/json"),
            ("X-Tossinvest-Account", "17"),
        ),
        body=body,
    )
    rotated = replace(
        first,
        headers=(
            ("Authorization", "Bearer rotated"),
            ("Content-Type", "application/json"),
            ("X-Tossinvest-Account", "17"),
        ),
    )
    changed_header = replace(
        first,
        headers=(
            ("Authorization", "Bearer first"),
            ("Content-Type", "application/json"),
            ("X-Tossinvest-Account", "18"),
        ),
    )

    assert canonical_toss_request_digest(first) == canonical_toss_request_digest(
        rotated
    )
    assert canonical_toss_request_digest(first) != canonical_toss_request_digest(
        changed_header
    )
    assert canonical_toss_request_digest(first) != canonical_toss_request_digest(
        replace(first, body=b'{"clientOrderId":"fixed","symbol":"MSFT"}')
    )


@pytest.mark.asyncio
async def test_pre_send_failure_is_recorded_without_claiming_acceptance() -> None:
    value = command()
    write_context = context(value)
    store = MemoryRecoveryStore()
    sender = Sender(store, [TossPreSendFailure()])

    with pytest.raises(TossUsCashWriterNotSent):
        await writer(value, write_context, store, sender).submit_locked(value)

    assert store.rows[value.id].state is TossRecoveryState.OPEN
    assert store.rows[value.id].replay_count == 0


@pytest.mark.asyncio
async def test_post_send_failure_stays_open_for_one_bounded_recovery() -> None:
    value = command()
    write_context = context(value)
    store = MemoryRecoveryStore()
    sender = Sender(
        store,
        [TossPostSendFailure(TossPostSendFailureKind.TIMEOUT)],
    )

    with pytest.raises(TossUsCashWriterUnknown):
        await writer(value, write_context, store, sender).submit_locked(value)

    assert store.rows[value.id].state is TossRecoveryState.OPEN
    assert store.rows[value.id].replay_count == 0


@pytest.mark.asyncio
async def test_exact_authority_is_required_before_persistence() -> None:
    value = command()
    write_context = context(value, writer_capability=False)
    store = MemoryRecoveryStore()
    sender = Sender(store, [success(value)])

    with pytest.raises(BrokerWriteDisabled, match="capability"):
        await writer(value, write_context, store, sender).submit_locked(value)

    assert store.rows == {}
    assert sender.calls == []


__all__ = (
    "Authority",
    "Clock",
    "MemoryRecoveryStore",
    "Sender",
    "Tokens",
    "command",
    "context",
    "success",
    "writer",
)
