from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from urllib.parse import parse_qsl
from uuid import UUID

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.binance_usdm.orders import (
    BinanceUsdmNormalOrderAuthority,
    BinanceUsdmNormalOrderClaim,
    BinanceUsdmNormalOrderRecord,
    BinanceUsdmNormalOrderState,
    BinanceUsdmOrderNotSent,
    BinanceUsdmOrderRejected,
    BinanceUsdmOrderRole,
    BinanceUsdmOrderService,
    BinanceUsdmOrderUnknown,
    BinanceUsdmPreSendFailure,
    BinanceUsdmSymbolFilters,
    binance_normal_client_order_id,
    build_binance_usdm_order_request,
)
from autotrader.integrations.brokers.common import (
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)
from autotrader.risk.v6 import MAX_LEVERAGE
from autotrader.shared.ids import new_uuid7

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Authority:
    def __init__(self, value: BinanceUsdmNormalOrderAuthority) -> None:
        self.value = value

    async def load(
        self, command: BrokerOrderCommand
    ) -> BinanceUsdmNormalOrderAuthority:
        del command
        return self.value


class MemoryStore:
    def __init__(self) -> None:
        self.rows: dict[str, BinanceUsdmNormalOrderRecord] = {}
        self.persisted_before_send = False

    async def prepare(
        self, record: BinanceUsdmNormalOrderRecord
    ) -> BinanceUsdmNormalOrderClaim:
        current = self.rows.get(record.client_order_id)
        if current is None:
            self.rows[record.client_order_id] = record
            return BinanceUsdmNormalOrderClaim(record=record, acquired=True)
        if (
            current.command_id != record.command_id
            or current.account_id != record.account_id
            or current.binding_id != record.binding_id
            or current.request_body != record.request_body
            or current.request_digest != record.request_digest
            or current.not_after != record.not_after
        ):
            raise ValueError("Binance USD-M persisted request mismatch")
        if current.state is BinanceUsdmNormalOrderState.NOT_SENT:
            claimed = replace(
                current,
                state=BinanceUsdmNormalOrderState.PREPARED,
                dispatch_count=current.dispatch_count + 1,
            )
            self.rows[record.client_order_id] = claimed
            return BinanceUsdmNormalOrderClaim(record=claimed, acquired=True)
        return BinanceUsdmNormalOrderClaim(record=current, acquired=False)

    async def load_by_client_id(
        self, client_order_id: str
    ) -> BinanceUsdmNormalOrderRecord | None:
        return self.rows.get(client_order_id)

    async def mark_not_sent(
        self, client_order_id: str, *, request_digest: bytes
    ) -> BinanceUsdmNormalOrderRecord:
        current = self.rows[client_order_id]
        assert current.request_digest == request_digest
        updated = replace(current, state=BinanceUsdmNormalOrderState.NOT_SENT)
        self.rows[client_order_id] = updated
        return updated

    async def finish(
        self,
        client_order_id: str,
        *,
        state: BinanceUsdmNormalOrderState,
        result: object | None,
    ) -> BinanceUsdmNormalOrderRecord:
        current = self.rows[client_order_id]
        updated = replace(current, state=state, result=result)
        updated.validate()
        self.rows[client_order_id] = updated
        return updated


@dataclass
class Sender:
    store: MemoryStore
    outcomes: list[BrokerResponse | BaseException]
    calls: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def send(self, request: BrokerRequest) -> BrokerResponse:
        self.store.persisted_before_send = bool(self.store.rows)
        self.calls.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def filters(**changes: object) -> BinanceUsdmSymbolFilters:
    values: dict[str, object] = {
        "tick_size": Decimal("0.10"),
        "step_size": Decimal("0.001"),
        "minimum_quantity": Decimal("0.001"),
        "minimum_notional": Decimal("5"),
        "captured_at": NOW,
    }
    values.update(changes)
    return BinanceUsdmSymbolFilters(**values)  # type: ignore[arg-type]


def command_and_authority(
    *,
    side: Side = Side.BUY,
    order_style: OrderStyle = OrderStyle.MARKET,
    quantity: Decimal = Decimal("0.002"),
    limit_price: Decimal | None = None,
    role: BinanceUsdmOrderRole = BinanceUsdmOrderRole.ENTRY,
    expected_leverage: int = 3,
    verified_leverage: int = 3,
    leverage_verified_at: datetime = NOW,
    symbol_filters: BinanceUsdmSymbolFilters | None = None,
    reference_price: Decimal = Decimal("60000"),
    authorized_reduce_quantity: Decimal = Decimal(),
    position_mode: str = "ONE_WAY",
    margin_type: str = "ISOLATED",
    auto_add_margin: bool = False,
) -> tuple[BrokerOrderCommand, BinanceUsdmNormalOrderAuthority]:
    command_id = new_uuid7()
    value = BrokerOrderCommand(
        id=command_id,
        order_id=new_uuid7(),
        account_id=new_uuid7(),
        instrument_id=new_uuid7(),
        command_type=CommandType.SUBMIT,
        target_aggregate_version=1,
        idempotency_key=f"v6-binance-{command_id.hex}",
        command_sequence=1,
        canonical_payload_hash=b"\x00" * 32,
        broker_client_order_id=binance_normal_client_order_id(command_id),
        target_broker_order_id=None,
        replaces_command_id=None,
        origin_type="STRATEGY",
        authority_class="SUBMIT_NEW_EXPOSURE",
        owner_runtime_instance_id=new_uuid7(),
        fencing_token=9,
        not_after=NOW + timedelta(minutes=2),
        side=side,
        order_style=order_style,
        quantity=quantity,
        limit_price=limit_price,
        time_in_force="GTC" if order_style is OrderStyle.LIMIT else "NONE",
        dispatch_attempted_at=NOW,
    )
    authority = BinanceUsdmNormalOrderAuthority(
        command_id=value.id,
        account_id=value.account_id,
        instrument_id=value.instrument_id,
        binding_id=new_uuid7(),
        binding_generation=9,
        policy_version_id=new_uuid7(),
        strategy_version="david-trullas-v6.0",
        writer_capability=True,
        account_enabled=True,
        binding_active=True,
        intent_locked=True,
        symbol="BTCUSDT",
        role=role,
        side=side,
        order_style=order_style,
        quantity=quantity,
        limit_price=limit_price,
        expected_leverage=expected_leverage,
        verified_leverage=verified_leverage,
        leverage_verified_at=leverage_verified_at,
        position_mode=position_mode,
        margin_type=margin_type,
        auto_add_margin=auto_add_margin,
        filters=filters() if symbol_filters is None else symbol_filters,
        notional_reference_price=reference_price,
        authorized_reduce_quantity=authorized_reduce_quantity,
    )
    request = build_binance_usdm_order_request(value, authority)
    return replace(
        value, canonical_payload_hash=sha256(request.body or b"").digest()
    ), authority


def order_response(
    value: BrokerOrderCommand,
    *,
    status: str = "FILLED",
    order_id: int = 811,
    executed_quantity: str = "0.002",
    cumulative_quote: str = "121",
    average_price: str = "60500",
    reduce_only: bool = False,
) -> BrokerResponse:
    return BrokerResponse(
        status=200,
        body=json.dumps(
            {
                "orderId": order_id,
                "clientOrderId": value.broker_client_order_id,
                "symbol": "BTCUSDT",
                "status": status,
                "side": value.side.value,
                "type": value.order_style.value,
                "origQty": str(value.quantity),
                "executedQty": executed_quantity,
                "cumQuote": cumulative_quote,
                "avgPrice": average_price,
                "reduceOnly": reduce_only,
                "positionSide": "BOTH",
            }
        ).encode(),
    )


def fill_response(
    order_id: int = 811,
    *,
    side: Side = Side.BUY,
) -> BrokerResponse:
    return BrokerResponse(
        status=200,
        body=json.dumps(
            [
                {
                    "id": 91,
                    "orderId": order_id,
                    "symbol": "BTCUSDT",
                    "side": side.value,
                    "qty": "0.001",
                    "price": "60000",
                    "commission": "0.012",
                    "commissionAsset": "USDT",
                    "realizedPnl": "0",
                    "time": 1787572800100,
                },
                {
                    "id": 92,
                    "orderId": order_id,
                    "symbol": "BTCUSDT",
                    "side": side.value,
                    "qty": "0.001",
                    "price": "61000",
                    "commission": "0.0122",
                    "commissionAsset": "USDT",
                    "realizedPnl": "0",
                    "time": 1787572800200,
                },
            ]
        ).encode(),
    )


def service(
    value: BrokerOrderCommand,
    authority: BinanceUsdmNormalOrderAuthority,
    store: MemoryStore,
    sender: Sender,
    *,
    clock: Clock | None = None,
) -> BinanceUsdmOrderService:
    del value
    return BinanceUsdmOrderService(
        authority=Authority(authority),
        store=store,
        sender=sender,
        clock=Clock() if clock is None else clock,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
async def test_persists_deterministic_one_way_request_before_network(
    side: Side,
) -> None:
    value, write_authority = command_and_authority(side=side)
    store = MemoryStore()
    sender = Sender(store, [order_response(value), fill_response(side=side)])

    result = await service(value, write_authority, store, sender).submit_locked(value)

    assert value.broker_client_order_id == f"v6-{value.id.hex}"
    assert len(value.broker_client_order_id) == 35
    assert store.persisted_before_send is True
    request = sender.calls[0]
    assert request.method == "POST"
    assert request.path == "/fapi/v1/order"
    assert dict(parse_qsl((request.body or b"").decode())) == {
        "symbol": "BTCUSDT",
        "side": side.value,
        "positionSide": "BOTH",
        "type": "MARKET",
        "quantity": "0.002",
        "newClientOrderId": value.broker_client_order_id,
        "newOrderRespType": "RESULT",
    }
    row = store.rows[value.broker_client_order_id]
    assert row.request_body == request.body
    assert row.request_digest == sha256(request.body or b"").digest()
    assert row.state is BinanceUsdmNormalOrderState.ACKNOWLEDGED
    assert result.broker_order_id == "BINANCE-USDM:811"


@pytest.mark.asyncio
async def test_rejects_non_deterministic_client_id_before_persistence() -> None:
    value, write_authority = command_and_authority()
    value = replace(value, broker_client_order_id="caller-selected")
    store = MemoryStore()
    sender = Sender(store, [order_response(value), fill_response()])

    with pytest.raises(BrokerWriteDisabled, match="client"):
        await service(value, write_authority, store, sender).submit_locked(value)

    assert store.rows == {}
    assert sender.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"quantity": Decimal("0.0015")}, "step"),
        ({"quantity": Decimal("0.000")}, "quantity"),
        (
            {
                "quantity": Decimal("0.001"),
                "reference_price": Decimal("4000"),
            },
            "notional",
        ),
        (
            {
                "order_style": OrderStyle.LIMIT,
                "limit_price": Decimal("60000.05"),
            },
            "tick",
        ),
        (
            {"symbol_filters": filters(captured_at=NOW - timedelta(seconds=31))},
            "filter",
        ),
    ],
)
async def test_exchange_filters_fail_closed(
    changes: dict[str, object], message: str
) -> None:
    value, write_authority = command_and_authority(**changes)  # type: ignore[arg-type]
    store = MemoryStore()
    sender = Sender(store, [order_response(value), fill_response()])

    with pytest.raises(BrokerWriteDisabled, match=message):
        await service(value, write_authority, store, sender).submit_locked(value)

    assert sender.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"expected_leverage": 0, "verified_leverage": 0}, "leverage"),
        (
            {
                "expected_leverage": MAX_LEVERAGE + 1,
                "verified_leverage": MAX_LEVERAGE + 1,
            },
            "leverage",
        ),
        ({"expected_leverage": 3, "verified_leverage": 2}, "leverage"),
        (
            {"leverage_verified_at": NOW - timedelta(seconds=31)},
            "leverage",
        ),
        ({"position_mode": "HEDGE"}, "ONE_WAY"),
        ({"margin_type": "CROSSED"}, "ISOLATED"),
        ({"auto_add_margin": True}, "margin"),
    ],
)
async def test_entry_requires_fresh_exact_leverage_and_account_authority(
    changes: dict[str, object], message: str
) -> None:
    value, write_authority = command_and_authority(**changes)  # type: ignore[arg-type]
    store = MemoryStore()
    sender = Sender(store, [order_response(value), fill_response()])

    with pytest.raises(BrokerWriteDisabled, match=message):
        await service(value, write_authority, store, sender).submit_locked(value)

    assert sender.calls == []


@pytest.mark.asyncio
async def test_emergency_exit_is_full_reduce_only_market_order() -> None:
    value, write_authority = command_and_authority(
        side=Side.SELL,
        quantity=Decimal("0.007"),
        role=BinanceUsdmOrderRole.EMERGENCY_CLOSE,
        authorized_reduce_quantity=Decimal("0.007"),
        leverage_verified_at=NOW - timedelta(days=1),
    )
    response = order_response(
        value,
        executed_quantity="0.007",
        cumulative_quote="420",
        average_price="60000",
        reduce_only=True,
    )
    fills = BrokerResponse(
        status=200,
        body=json.dumps(
            [
                {
                    "id": 99,
                    "orderId": 811,
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "qty": "0.007",
                    "price": "60000",
                    "commission": "0.084",
                    "commissionAsset": "USDT",
                    "realizedPnl": "7",
                    "time": 1787572800100,
                }
            ]
        ).encode(),
    )
    store = MemoryStore()
    sender = Sender(store, [response, fills])

    await service(value, write_authority, store, sender).submit_locked(value)

    parameters = dict(parse_qsl((sender.calls[0].body or b"").decode()))
    assert parameters["reduceOnly"] == "true"
    assert parameters["positionSide"] == "BOTH"
    assert parameters["type"] == "MARKET"


@pytest.mark.asyncio
async def test_reduce_only_close_is_exempt_from_minimum_notional() -> None:
    value, write_authority = command_and_authority(
        side=Side.SELL,
        quantity=Decimal("0.001"),
        role=BinanceUsdmOrderRole.EMERGENCY_CLOSE,
        authorized_reduce_quantity=Decimal("0.001"),
        reference_price=Decimal("4000"),
    )
    response = order_response(
        value,
        executed_quantity="0.001",
        cumulative_quote="4",
        average_price="4000",
        reduce_only=True,
    )
    fills = BrokerResponse(
        status=200,
        body=json.dumps(
            [
                {
                    "id": 100,
                    "orderId": 811,
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "qty": "0.001",
                    "price": "4000",
                    "commission": "0.0016",
                    "commissionAsset": "USDT",
                    "realizedPnl": "-1",
                    "time": 1787572800100,
                }
            ]
        ).encode(),
    )
    store = MemoryStore()
    sender = Sender(store, [response, fills])

    result = await service(value, write_authority, store, sender).submit_locked(value)

    assert result.provider_state == "FILLED"
    assert (
        dict(parse_qsl((sender.calls[0].body or b"").decode()))["reduceOnly"] == "true"
    )


@pytest.mark.asyncio
async def test_pre_send_failure_is_durably_safe_to_retry_exact_body() -> None:
    value, write_authority = command_and_authority()
    store = MemoryStore()
    first_sender = Sender(store, [BinanceUsdmPreSendFailure()])

    with pytest.raises(BinanceUsdmOrderNotSent):
        await service(value, write_authority, store, first_sender).submit_locked(value)

    row = store.rows[value.broker_client_order_id]
    assert row.state is BinanceUsdmNormalOrderState.NOT_SENT
    first_body = first_sender.calls[0].body
    retry_sender = Sender(store, [order_response(value), fill_response()])

    await service(value, write_authority, store, retry_sender).submit_locked(value)

    assert retry_sender.calls[0].body == first_body
    assert store.rows[value.broker_client_order_id].dispatch_count == 2


@pytest.mark.asyncio
async def test_partial_fill_decodes_cumulative_fills_and_commissions() -> None:
    value, write_authority = command_and_authority()
    partial = order_response(
        value,
        status="PARTIALLY_FILLED",
        executed_quantity="0.001",
        cumulative_quote="60",
        average_price="60000",
    )
    one_fill = BrokerResponse(
        status=200,
        body=json.dumps(json.loads(fill_response().body)[:1]).encode(),
    )
    store = MemoryStore()
    sender = Sender(store, [partial, one_fill])

    result = await service(value, write_authority, store, sender).submit_locked(value)

    assert result.provider_state == "PARTIALLY_FILLED"
    assert result.cumulative_filled_quantity == Decimal("0.001")
    assert result.cumulative_quote_quantity == Decimal("60")
    assert result.average_fill_price == Decimal("60000")
    assert result.commissions == (("USDT", Decimal("0.012")),)
    assert tuple(fill.trade_id for fill in result.fills) == (91,)


@pytest.mark.asyncio
async def test_authoritative_post_rejection_is_terminal() -> None:
    value, write_authority = command_and_authority()
    store = MemoryStore()
    sender = Sender(
        store,
        [BrokerResponse(status=400, body=b'{"code":-1111,"msg":"bad"}')],
    )

    with pytest.raises(BinanceUsdmOrderRejected) as raised:
        await service(value, write_authority, store, sender).submit_locked(value)

    assert raised.value.args == ("Binance USD-M order was rejected",)
    assert store.rows[value.broker_client_order_id].state is (
        BinanceUsdmNormalOrderState.REJECTED
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [-1000, -1006, -1007, -4116])
async def test_unknown_or_duplicate_provider_code_is_never_rejected(
    code: int,
) -> None:
    value, write_authority = command_and_authority()
    store = MemoryStore()
    sender = Sender(
        store,
        [
            BrokerResponse(
                status=400,
                body=json.dumps({"code": code, "msg": "do-not-persist"}).encode(),
            )
        ],
    )

    with pytest.raises(BinanceUsdmOrderUnknown):
        await service(value, write_authority, store, sender).submit_locked(value)

    assert store.rows[value.broker_client_order_id].state is (
        BinanceUsdmNormalOrderState.AMBIGUOUS
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [b'{"code":-9999,"msg":"new provider code"}', b"proxy error"],
)
async def test_unknown_or_malformed_4xx_fails_ambiguous(body: bytes) -> None:
    value, write_authority = command_and_authority()
    store = MemoryStore()
    sender = Sender(store, [BrokerResponse(status=400, body=body)])

    with pytest.raises(BinanceUsdmOrderUnknown):
        await service(value, write_authority, store, sender).submit_locked(value)

    assert store.rows[value.broker_client_order_id].state is (
        BinanceUsdmNormalOrderState.AMBIGUOUS
    )


@pytest.mark.asyncio
async def test_malformed_success_is_unknown_without_payload_leak() -> None:
    value, write_authority = command_and_authority()
    store = MemoryStore()
    sender = Sender(
        store,
        [BrokerResponse(status=200, body=b'{"secret":"do-not-leak"}')],
    )

    with pytest.raises(BinanceUsdmOrderUnknown) as raised:
        await service(value, write_authority, store, sender).submit_locked(value)

    assert "secret" not in str(raised.value)
    assert store.rows[value.broker_client_order_id].state is (
        BinanceUsdmNormalOrderState.AMBIGUOUS
    )


def test_client_id_is_deterministic_and_rejects_non_uuid7() -> None:
    command_id = new_uuid7()
    assert binance_normal_client_order_id(command_id) == f"v6-{command_id.hex}"
    with pytest.raises(ValueError, match="UUIDv7"):
        binance_normal_client_order_id(UUID(int=1))
