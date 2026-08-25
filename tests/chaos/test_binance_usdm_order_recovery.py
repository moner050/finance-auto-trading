from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from autotrader.integrations.brokers.binance_usdm.orders import (
    BinanceUsdmNormalOrderRecord,
    BinanceUsdmNormalOrderState,
    BinanceUsdmOrderUnknown,
    build_binance_usdm_order_request,
)
from autotrader.integrations.brokers.binance_usdm.transport import (
    BinanceUsdmAmbiguousWrite,
)
from autotrader.integrations.brokers.common import BrokerResponse
from tests.unit.integrations.brokers.binance_usdm.test_orders import (
    Clock,
    MemoryStore,
    Sender,
    command_and_authority,
    fill_response,
    order_response,
    service,
)


@pytest.mark.asyncio
async def test_post_send_timeout_recovers_only_by_exact_client_id() -> None:
    value, authority = command_and_authority()
    store = MemoryStore()
    first = Sender(store, [BinanceUsdmAmbiguousWrite(503)])

    with pytest.raises(BinanceUsdmOrderUnknown):
        await service(value, authority, store, first).submit_locked(value)

    assert store.rows[value.broker_client_order_id].state is (
        BinanceUsdmNormalOrderState.AMBIGUOUS
    )
    recovery = Sender(store, [order_response(value), fill_response()])

    result = await service(value, authority, store, recovery).recover_by_client_id(
        value.broker_client_order_id
    )

    assert result.recovered is True
    assert [request.method for request in recovery.calls] == ["GET", "GET"]
    assert recovery.calls[0].path == (
        "/fapi/v1/order?symbol=BTCUSDT&origClientOrderId="
        f"{value.broker_client_order_id}"
    )
    assert "orderId=811" in recovery.calls[1].path
    assert store.rows[value.broker_client_order_id].state is (
        BinanceUsdmNormalOrderState.ACKNOWLEDGED
    )


@pytest.mark.asyncio
async def test_duplicate_client_id_response_recovers_existing_order() -> None:
    value, authority = command_and_authority()
    store = MemoryStore()
    sender = Sender(
        store,
        [
            BrokerResponse(
                status=400,
                body=b'{"code":-4116,"msg":"duplicate clientOrderId"}',
            ),
            order_response(value),
            fill_response(),
        ],
    )
    order_service = service(value, authority, store, sender)

    with pytest.raises(BinanceUsdmOrderUnknown):
        await order_service.submit_locked(value)
    recovered = await order_service.recover_by_client_id(value.broker_client_order_id)

    assert recovered.recovered is True
    assert [request.method for request in sender.calls] == ["POST", "GET", "GET"]


@pytest.mark.asyncio
async def test_not_found_during_eventual_consistency_never_reposts() -> None:
    value, authority = command_and_authority()
    store = MemoryStore()
    first = Sender(store, [BinanceUsdmAmbiguousWrite(503)])
    with pytest.raises(BinanceUsdmOrderUnknown):
        await service(value, authority, store, first).submit_locked(value)
    recovery = Sender(
        store,
        [
            BrokerResponse(
                status=400,
                body=b'{"code":-2013,"msg":"Order does not exist."}',
            )
        ],
    )

    with pytest.raises(BinanceUsdmOrderUnknown, match="not yet found"):
        await service(value, authority, store, recovery).recover_by_client_id(
            value.broker_client_order_id
        )

    assert len(recovery.calls) == 1
    assert recovery.calls[0].method == "GET"
    assert store.rows[value.broker_client_order_id].state is (
        BinanceUsdmNormalOrderState.AMBIGUOUS
    )


@pytest.mark.asyncio
async def test_provider_error_during_recovery_stays_ambiguous() -> None:
    value, authority = command_and_authority()
    store = MemoryStore()
    first = Sender(store, [BinanceUsdmAmbiguousWrite(503)])
    with pytest.raises(BinanceUsdmOrderUnknown):
        await service(value, authority, store, first).submit_locked(value)
    recovery = Sender(
        store,
        [BrokerResponse(status=500, body=b'{"error":"provider unavailable"}')],
    )

    with pytest.raises(BinanceUsdmOrderUnknown) as raised:
        await service(value, authority, store, recovery).recover_by_client_id(
            value.broker_client_order_id
        )

    assert "provider unavailable" not in str(raised.value)
    assert all(request.method == "GET" for request in recovery.calls)
    assert store.rows[value.broker_client_order_id].state is (
        BinanceUsdmNormalOrderState.AMBIGUOUS
    )


@pytest.mark.asyncio
async def test_restart_after_ack_returns_persisted_result_without_network() -> None:
    value, authority = command_and_authority()
    store = MemoryStore()
    first = Sender(store, [order_response(value), fill_response()])
    accepted = await service(value, authority, store, first).submit_locked(value)
    restarted = Sender(store, [])

    result = await service(value, authority, store, restarted).submit_locked(value)

    assert result == accepted
    assert restarted.calls == []


@pytest.mark.asyncio
async def test_restart_after_deadline_returns_persisted_ack_without_authority() -> None:
    value, authority = command_and_authority()
    store = MemoryStore()
    first = Sender(store, [order_response(value), fill_response()])
    accepted = await service(value, authority, store, first).submit_locked(value)
    restarted = Sender(store, [])
    stale_clock = Clock(value.not_after + timedelta(seconds=1))

    result = await service(
        value,
        authority,
        store,
        restarted,
        clock=stale_clock,
    ).submit_locked(value)

    assert result == accepted
    assert restarted.calls == []


@pytest.mark.asyncio
async def test_restart_after_prepare_queries_and_never_posts() -> None:
    value, authority = command_and_authority()
    store = MemoryStore()
    request = build_binance_usdm_order_request(value, authority)
    record = BinanceUsdmNormalOrderRecord.prepared(
        command=value,
        authority=authority,
        request=request,
    )
    await store.prepare(record)
    restarted = Sender(store, [order_response(value), fill_response()])

    result = await service(value, authority, store, restarted).submit_locked(value)

    assert result.recovered is True
    assert [request.method for request in restarted.calls] == ["GET", "GET"]


@pytest.mark.asyncio
async def test_restart_after_deadline_still_queries_ambiguous_order() -> None:
    value, authority = command_and_authority()
    store = MemoryStore()
    first = Sender(store, [BinanceUsdmAmbiguousWrite(503)])
    with pytest.raises(BinanceUsdmOrderUnknown):
        await service(value, authority, store, first).submit_locked(value)
    restarted = Sender(store, [order_response(value), fill_response()])
    stale_clock = Clock(value.not_after + timedelta(seconds=1))

    recovered = await service(
        value,
        authority,
        store,
        restarted,
        clock=stale_clock,
    ).submit_locked(value)

    assert recovered.recovered is True
    assert [request.method for request in restarted.calls] == ["GET", "GET"]


@pytest.mark.asyncio
async def test_query_identity_mismatch_is_never_adopted() -> None:
    value, authority = command_and_authority()
    store = MemoryStore()
    first = Sender(store, [BinanceUsdmAmbiguousWrite(503)])
    with pytest.raises(BinanceUsdmOrderUnknown):
        await service(value, authority, store, first).submit_locked(value)
    wrong = json.loads(order_response(value).body)
    wrong["clientOrderId"] = "v6-wrong"
    recovery = Sender(
        store,
        [BrokerResponse(status=200, body=json.dumps(wrong).encode())],
    )

    with pytest.raises(BinanceUsdmOrderUnknown):
        await service(value, authority, store, recovery).recover_by_client_id(
            value.broker_client_order_id
        )

    assert store.rows[value.broker_client_order_id].result is None


@pytest.mark.asyncio
async def test_terminal_unknown_record_is_not_reopened() -> None:
    value, authority = command_and_authority()
    store = MemoryStore()
    request = build_binance_usdm_order_request(value, authority)
    record = BinanceUsdmNormalOrderRecord.prepared(
        command=value,
        authority=authority,
        request=request,
    )
    await store.prepare(record)
    store.rows[value.broker_client_order_id] = replace(
        record, state=BinanceUsdmNormalOrderState.UNKNOWN
    )
    restarted = Sender(store, [])

    with pytest.raises(BinanceUsdmOrderUnknown, match="terminal"):
        await service(value, authority, store, restarted).submit_locked(value)

    assert restarted.calls == []
