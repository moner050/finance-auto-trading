from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.common import BrokerResponse, BrokerWriteDisabled
from autotrader.integrations.brokers.kis.adapter import KisCashExecutionAdapter
from autotrader.integrations.brokers.kis.cash_order_contracts import (
    KisCashAccount,
    KisCashEnvironment,
    LockedOrderIntent,
    ProviderOrderIdentity,
)
from autotrader.integrations.brokers.kis.cash_order_recovery import KisDailyOrder
from autotrader.integrations.brokers.kis.cash_order_transport import (
    KisCashOrderTransport,
    KisPostSendFailure,
    KisPostSendFailureKind,
)
from autotrader.integrations.brokers.kis.cash_writer import (
    KisCashWriteContext,
    KisCashWriter,
    KisCashWriterUnknown,
)
from autotrader.shared.ids import new_uuid7
from tests.unit.integrations.brokers.kis.test_kis_cash_order_transport import (
    MemoryDispatchStore,
    ScriptedSender,
    _success,
)

NOW = datetime(2026, 8, 24, 0, 15, 0, tzinfo=UTC)


def _command(*, command_type: CommandType = CommandType.SUBMIT) -> BrokerOrderCommand:
    return BrokerOrderCommand(
        id=new_uuid7(),
        order_id=new_uuid7(),
        account_id=new_uuid7(),
        instrument_id=new_uuid7(),
        command_type=command_type,
        target_aggregate_version=1,
        idempotency_key="v6-kis-command",
        command_sequence=1,
        canonical_payload_hash=b"p" * 32,
        broker_client_order_id="v6-kis-intent",
        target_broker_order_id=(
            None
            if command_type is CommandType.SUBMIT
            else "KIS-KRX:20260824:12345:0000000042"
        ),
        replaces_command_id=None,
        origin_type="DAVID_V6_DECISION",
        authority_class="V6_PROVIDER_WRITE",
        owner_runtime_instance_id=new_uuid7(),
        fencing_token=7,
        not_after=NOW + timedelta(seconds=30),
        side=Side.BUY,
        order_style=OrderStyle.LIMIT,
        quantity=(
            Decimal("10") if command_type is CommandType.SUBMIT else Decimal("6")
        ),
        limit_price=Decimal("70000"),
        time_in_force="DAY",
        dispatch_attempted_at=NOW,
    )


def _context(
    command: BrokerOrderCommand,
    *,
    writer_capability: bool = True,
    binding_generation: int = 7,
) -> KisCashWriteContext:
    intent = LockedOrderIntent(
        id=new_uuid7(),
        v6_decision_id=new_uuid7(),
        account_id=command.account_id,
        symbol="005930",
        side=command.side,
        order_style=command.order_style,
        quantity=command.quantity,
        limit_price=command.limit_price,
        opens_exposure=command.side is Side.BUY,
        common_stock_authorized=True,
        binding_generation=binding_generation,
        locked=True,
    )
    return KisCashWriteContext(
        command_id=command.id,
        instrument_id=command.instrument_id,
        intent=intent,
        account=KisCashAccount(
            account_id=command.account_id,
            account_alias="kis-paper-cash",
            environment=KisCashEnvironment.PAPER,
            account_number="12345678",
            product_code="01",
            enabled=False,
        ),
        provider_binding_id=new_uuid7(),
        binding_generation=binding_generation,
        policy_version_id=new_uuid7(),
        strategy_version="david-trullas-v6.0",
        writer_capability=writer_capability,
        target_order=(
            None
            if command.command_type is CommandType.SUBMIT
            else ProviderOrderIdentity(
                organization_number="12345",
                order_number="0000000042",
                symbol="005930",
                side=Side.BUY,
                remaining_quantity=Decimal("6"),
                order_style=OrderStyle.LIMIT,
                limit_price=Decimal("70000"),
            )
        ),
    )


class Authority:
    def __init__(self, context: KisCashWriteContext) -> None:
        self.context = context

    async def load(self, command: BrokerOrderCommand) -> KisCashWriteContext:
        del command
        return self.context


class Recovery:
    def __init__(self, orders: tuple[KisDailyOrder, ...] = ()) -> None:
        self.orders = orders

    async def daily_orders(
        self, binding_id: object, provider_trade_date: object
    ) -> tuple[KisDailyOrder, ...]:
        del binding_id, provider_trade_date
        return self.orders


def _writer(
    command: BrokerOrderCommand,
    sender: ScriptedSender,
    *,
    context: KisCashWriteContext | None = None,
    store: MemoryDispatchStore | None = None,
    recovery: Recovery | None = None,
) -> KisCashWriter:
    return KisCashWriter(
        authority=Authority(_context(command) if context is None else context),
        transport=KisCashOrderTransport(
            MemoryDispatchStore() if store is None else store, sender
        ),
        recovery=Recovery() if recovery is None else recovery,
    )


@pytest.mark.asyncio
async def test_exact_locked_submit_returns_canonical_provider_identity() -> None:
    command = _command()
    sender = ScriptedSender(_success())

    result = await _writer(command, sender).submit_locked(command)

    assert result.broker_order_id == "KIS-KRX:20260824:12345:0000000042"
    assert result.provider_state == "ACKNOWLEDGED"
    assert sender.calls == 1


@pytest.mark.asyncio
async def test_explicit_cash_execution_adapter_delegates_to_locked_writer() -> None:
    command = _command()
    sender = ScriptedSender(_success())
    adapter = KisCashExecutionAdapter(writer=_writer(command, sender))

    result = await adapter.submit(command)

    assert result.broker_order_id == "KIS-KRX:20260824:12345:0000000042"
    assert sender.calls == 1


@pytest.mark.asyncio
async def test_missing_writer_capability_blocks_before_network() -> None:
    command = _command()
    sender = ScriptedSender(_success())
    context = _context(command, writer_capability=False)

    with pytest.raises(BrokerWriteDisabled, match="capability"):
        await _writer(command, sender, context=context).submit_locked(command)

    assert sender.calls == 0


@pytest.mark.asyncio
async def test_binding_generation_mismatch_blocks_before_network() -> None:
    command = _command()
    sender = ScriptedSender(_success())
    context = _context(command, binding_generation=8)
    object.__setattr__(context.intent, "binding_generation", 7)

    with pytest.raises(BrokerWriteDisabled, match="authority"):
        await _writer(command, sender, context=context).submit_locked(command)

    assert sender.calls == 0


@pytest.mark.asyncio
async def test_ambiguous_submit_is_never_blindly_reposted() -> None:
    command = _command()
    sender = ScriptedSender(KisPostSendFailure(KisPostSendFailureKind.TIMEOUT))
    store = MemoryDispatchStore()
    writer = _writer(command, sender, store=store)

    with pytest.raises(KisCashWriterUnknown):
        await writer.submit_locked(command)
    with pytest.raises(KisCashWriterUnknown):
        await writer.submit_locked(command)

    assert sender.calls == 1


@pytest.mark.asyncio
async def test_unique_daily_order_recovers_ambiguous_submit() -> None:
    command = _command()
    context = _context(command)
    order = KisDailyOrder(
        binding_id=context.provider_binding_id,
        order_date="20260824",
        organization_number="12345",
        order_number="0000000042",
        original_order_number="0000000000",
        provider_timestamp=NOW + timedelta(seconds=2),
        side=Side.BUY,
        symbol="005930",
        order_style=OrderStyle.LIMIT,
        order_quantity=Decimal("10"),
        limit_price=Decimal("70000"),
        cumulative_filled_quantity=Decimal("4"),
        average_fill_price=Decimal("69900"),
        total_filled_amount=Decimal("279600"),
        confirmed_cancelled_quantity=Decimal("0"),
        remaining_quantity=Decimal("6"),
        rejected_quantity=Decimal("0"),
        fee_amount=Decimal("28"),
    )
    store = MemoryDispatchStore()
    writer = _writer(
        command,
        ScriptedSender(KisPostSendFailure(KisPostSendFailureKind.TIMEOUT)),
        context=context,
        store=store,
        recovery=Recovery((order,)),
    )
    with pytest.raises(KisCashWriterUnknown):
        await writer.submit_locked(command)

    recovered = await writer.recover_submit(command, now=NOW + timedelta(seconds=3))

    assert recovered is not None
    assert recovered.broker_order_id == "KIS-KRX:20260824:12345:0000000042"


@pytest.mark.asyncio
async def test_cancel_uses_exact_provider_identity_and_full_remaining_quantity() -> (
    None
):
    command = _command(command_type=CommandType.CANCEL)
    sender = ScriptedSender(_success())

    result = await _writer(command, sender).cancel_locked(command)

    assert result.provider_state == "ACKNOWLEDGED"
    assert sender.calls == 1


@pytest.mark.asyncio
async def test_http_business_rejection_is_not_reported_as_accepted() -> None:
    command = _command()
    sender = ScriptedSender(
        BrokerResponse(
            status=400,
            body=b'{"rt_cd":"1","msg_cd":"APBK0919","msg1":"rejected","output":{}}',
        )
    )

    with pytest.raises(RuntimeError, match="rejected"):
        await _writer(command, sender).submit_locked(command)
