from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from autotrader.integrations.brokers.binance_usdm.algo_orders import (
    BinanceUsdmAlgoOrderState,
    BinanceUsdmProtectionUnknown,
    binance_protection_client_algo_id,
)
from autotrader.integrations.brokers.binance_usdm.orders import (
    BinanceUsdmOrderUnknown,
)
from autotrader.integrations.brokers.binance_usdm.transport import (
    BinanceUsdmAmbiguousWrite,
)
from autotrader.integrations.brokers.common import BrokerRequest, BrokerResponse
from tests.unit.integrations.brokers.binance_usdm.test_algo_orders import (
    NOW,
    Clock,
    EmergencyOrders,
    MemoryStore,
    SafetyActions,
    Sender,
    algo_response,
    emergency_command,
    emergency_result,
    entry_fill,
    make_service,
    risk_authority,
)


class DeadlineCrossingSender(Sender):
    def __init__(
        self,
        store: MemoryStore,
        outcomes: list[BrokerResponse | BaseException],
        *,
        clock: Clock,
        cross_on_call: int,
    ) -> None:
        super().__init__(store, outcomes)
        self.clock = clock
        self.cross_on_call = cross_on_call

    async def send(self, request: BrokerRequest) -> BrokerResponse:
        try:
            return await super().send(request)
        finally:
            if len(self.calls) == self.cross_on_call:
                self.clock.now = NOW + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_post_timeout_recovers_only_by_exact_client_algo_id() -> None:
    fill = entry_fill()
    store = MemoryStore()
    sender = Sender(store, [BinanceUsdmAmbiguousWrite(503)])
    service = make_service(store, sender, EmergencyOrders([]), SafetyActions())

    with pytest.raises(BinanceUsdmProtectionUnknown, match="unknown"):
        await service.protect_first_fill(fill, risk_authority())

    client_algo_id = binance_protection_client_algo_id(fill.entry_command_id)
    assert store.rows[client_algo_id].state is BinanceUsdmAlgoOrderState.AMBIGUOUS
    assert len(sender.calls) == 1
    assert sender.calls[0].method == "POST"

    sender.outcomes.append(algo_response(fill, "59000"))
    recovered = await service.recover_by_client_algo_id(client_algo_id)

    assert recovered.recovered is True
    assert len(sender.calls) == 2
    assert sender.calls[1].method == "GET"
    assert sender.calls[1].path == (f"/fapi/v1/algoOrder?clientAlgoId={client_algo_id}")
    assert all(call.method != "POST" for call in sender.calls[1:])


@pytest.mark.asyncio
async def test_not_found_before_deadline_never_allows_repost() -> None:
    fill = entry_fill()
    store = MemoryStore()
    sender = Sender(
        store,
        [
            BinanceUsdmAmbiguousWrite(503),
            BrokerResponse(status=404, body=b'{"code":-2013,"msg":"not found"}'),
        ],
    )
    service = make_service(store, sender, EmergencyOrders([]), SafetyActions())

    with pytest.raises(BinanceUsdmProtectionUnknown):
        await service.protect_first_fill(fill, risk_authority())
    with pytest.raises(BinanceUsdmProtectionUnknown, match="not yet found"):
        await service.recover_by_client_algo_id(
            binance_protection_client_algo_id(fill.entry_command_id)
        )

    assert [call.method for call in sender.calls] == ["POST", "GET"]


@pytest.mark.asyncio
async def test_missing_protection_at_deadline_emergency_closes_and_halts() -> None:
    fill = entry_fill()
    clock = Clock()
    store = MemoryStore()
    sender = Sender(
        store,
        [
            BinanceUsdmAmbiguousWrite(503),
            BrokerResponse(status=404, body=b'{"code":-2013,"msg":"not found"}'),
        ],
    )
    close_command = emergency_command(fill, quantity=Decimal("0.003"))
    close_result = emergency_result(close_command)
    emergency = EmergencyOrders([close_result], command=close_command)
    safety = SafetyActions()
    service = make_service(store, sender, emergency, safety, clock=clock)

    with pytest.raises(BinanceUsdmProtectionUnknown):
        await service.protect_first_fill(fill, risk_authority())
    clock.now = NOW + timedelta(seconds=5)

    with pytest.raises(BinanceUsdmProtectionUnknown, match="emergency close"):
        await service.recover_by_client_algo_id(
            binance_protection_client_algo_id(fill.entry_command_id)
        )

    row = store.rows[binance_protection_client_algo_id(fill.entry_command_id)]
    assert row.state is BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED
    assert row.result is not None
    assert row.result.emergency_close == close_result
    assert emergency.prepared == [fill]
    assert emergency.submitted == [close_command]
    assert emergency.confirmed_zero == [fill]
    assert safety.canceled == [fill.binding_id]
    assert safety.halted == [(fill.binding_id, "BINANCE_PROTECTION_DEADLINE")]
    assert [call.method for call in sender.calls] == ["POST", "GET"]


@pytest.mark.asyncio
async def test_emergency_unknown_is_recovered_by_normal_client_id_then_halts() -> None:
    fill = entry_fill()
    clock = Clock(NOW + timedelta(seconds=5))
    store = MemoryStore()
    sender = Sender(store, [])
    close_command = emergency_command(fill)
    close_result = emergency_result(close_command)
    emergency = EmergencyOrders(
        [BinanceUsdmOrderUnknown("unknown"), close_result],
        command=close_command,
    )
    safety = SafetyActions()
    service = make_service(store, sender, emergency, safety, clock=clock)

    with pytest.raises(BinanceUsdmProtectionUnknown, match="emergency close"):
        await service.protect_first_fill(fill, risk_authority())

    assert emergency.prepared == [fill]
    assert emergency.submitted == [close_command]
    assert emergency.recovered == [close_command.broker_client_order_id]
    assert safety.halted == [(fill.binding_id, "BINANCE_PROTECTION_DEADLINE")]


@pytest.mark.asyncio
async def test_slow_recovery_crossing_deadline_enters_fail_safe() -> None:
    fill = entry_fill()
    clock = Clock()
    store = MemoryStore()
    sender = DeadlineCrossingSender(
        store,
        [
            BinanceUsdmAmbiguousWrite(503),
            BrokerResponse(status=404, body=b'{"code":-2013,"msg":"not found"}'),
        ],
        clock=clock,
        cross_on_call=2,
    )
    close_command = emergency_command(fill)
    emergency = EmergencyOrders(
        [emergency_result(close_command)],
        command=close_command,
    )
    safety = SafetyActions()
    service = make_service(store, sender, emergency, safety, clock=clock)

    with pytest.raises(BinanceUsdmProtectionUnknown):
        await service.protect_first_fill(fill, risk_authority())
    with pytest.raises(BinanceUsdmProtectionUnknown, match="emergency close"):
        await service.recover_by_client_algo_id(
            binance_protection_client_algo_id(fill.entry_command_id)
        )

    assert emergency.confirmed_zero == [fill]
    assert safety.halted == [(fill.binding_id, "BINANCE_PROTECTION_DEADLINE")]


@pytest.mark.asyncio
async def test_post_timeout_crossing_deadline_enters_fail_safe_immediately() -> None:
    fill = entry_fill()
    clock = Clock()
    store = MemoryStore()
    sender = DeadlineCrossingSender(
        store,
        [BinanceUsdmAmbiguousWrite(503)],
        clock=clock,
        cross_on_call=1,
    )
    close_command = emergency_command(fill)
    emergency = EmergencyOrders(
        [emergency_result(close_command)],
        command=close_command,
    )
    safety = SafetyActions()

    with pytest.raises(BinanceUsdmProtectionUnknown, match="emergency close"):
        await make_service(
            store,
            sender,
            emergency,
            safety,
            clock=clock,
        ).protect_first_fill(fill, risk_authority())

    client_algo_id = binance_protection_client_algo_id(fill.entry_command_id)
    assert (
        store.rows[client_algo_id].state is BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED
    )
    assert emergency.confirmed_zero == [fill]
    assert safety.halted == [(fill.binding_id, "BINANCE_PROTECTION_DEADLINE")]


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [False, True])
async def test_late_successful_acknowledgement_never_becomes_active(
    recover: bool,
) -> None:
    fill = entry_fill()
    clock = Clock()
    store = MemoryStore()
    outcomes: list[BrokerResponse | BaseException]
    if recover:
        outcomes = [BinanceUsdmAmbiguousWrite(503), algo_response(fill, "59000")]
        cross_on_call = 2
    else:
        outcomes = [algo_response(fill, "59000")]
        cross_on_call = 1
    sender = DeadlineCrossingSender(
        store,
        outcomes,
        clock=clock,
        cross_on_call=cross_on_call,
    )
    close_command = emergency_command(fill)
    emergency = EmergencyOrders(
        [emergency_result(close_command)],
        command=close_command,
    )
    safety = SafetyActions()
    service = make_service(store, sender, emergency, safety, clock=clock)

    if recover:
        with pytest.raises(BinanceUsdmProtectionUnknown):
            await service.protect_first_fill(fill, risk_authority())
        operation = service.recover_by_client_algo_id(
            binance_protection_client_algo_id(fill.entry_command_id)
        )
    else:
        operation = service.protect_first_fill(fill, risk_authority())

    with pytest.raises(BinanceUsdmProtectionUnknown, match="late protection"):
        await operation

    row = store.rows[binance_protection_client_algo_id(fill.entry_command_id)]
    assert row.state is BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED
    assert row.result is not None
    assert row.result.provider_algo_id == "BINANCE-USDM-ALGO:2146760"
    assert emergency.confirmed_zero == [fill]


@pytest.mark.asyncio
async def test_unknown_fail_safe_can_resume_until_zero_position_is_proven() -> None:
    fill = entry_fill()
    clock = Clock(NOW + timedelta(seconds=5))
    store = MemoryStore()
    close_command = emergency_command(fill)
    close_result = emergency_result(close_command)
    emergency = EmergencyOrders(
        [close_result, close_result],
        command=close_command,
        zero_results=[False, True],
    )
    safety = SafetyActions()
    service = make_service(store, Sender(store, []), emergency, safety, clock=clock)

    with pytest.raises(RuntimeError, match="outcome is unknown"):
        await service.protect_first_fill(fill, risk_authority())

    client_algo_id = binance_protection_client_algo_id(fill.entry_command_id)
    assert store.rows[client_algo_id].state is BinanceUsdmAlgoOrderState.UNKNOWN

    recovered = await service.recover_by_client_algo_id(client_algo_id)

    assert recovered.state is BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED
    assert emergency.prepared == [fill, fill]
    assert emergency.confirmed_zero == [fill, fill]
    assert len(safety.halted) == 2
