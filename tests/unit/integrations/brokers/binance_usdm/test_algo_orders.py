from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qsl

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.binance_usdm.algo_orders import (
    BinanceUsdmAlgoOrderClaim,
    BinanceUsdmAlgoOrderRecord,
    BinanceUsdmAlgoOrderState,
    BinanceUsdmProtectionRejected,
    BinanceUsdmProtectionService,
    EntryFill,
    ProtectionResult,
    binance_protection_client_algo_id,
)
from autotrader.integrations.brokers.binance_usdm.orders import (
    BrokerWriteResult,
    binance_normal_client_order_id,
)
from autotrader.integrations.brokers.common import (
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)
from autotrader.risk.v6 import V6RiskAuthority
from autotrader.shared.ids import new_uuid7

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class MemoryStore:
    def __init__(self) -> None:
        self.rows: dict[str, BinanceUsdmAlgoOrderRecord] = {}
        self.persisted_before_send = False

    async def prepare(
        self, record: BinanceUsdmAlgoOrderRecord
    ) -> BinanceUsdmAlgoOrderClaim:
        current = self.rows.get(record.client_algo_id)
        if current is None:
            self.rows[record.client_algo_id] = record
            return BinanceUsdmAlgoOrderClaim(record=record, acquired=True)
        return BinanceUsdmAlgoOrderClaim(record=current, acquired=False)

    async def load_by_client_algo_id(
        self, client_algo_id: str
    ) -> BinanceUsdmAlgoOrderRecord | None:
        return self.rows.get(client_algo_id)

    async def finish(
        self,
        client_algo_id: str,
        *,
        state: BinanceUsdmAlgoOrderState,
        result: ProtectionResult | None,
    ) -> BinanceUsdmAlgoOrderRecord:
        current = self.rows[client_algo_id]
        updated = replace(current, state=state, result=result)
        updated.validate()
        self.rows[client_algo_id] = updated
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


@dataclass
class EmergencyOrders:
    outcomes: list[BrokerWriteResult | BaseException]
    command: BrokerOrderCommand | None = None
    zero_results: list[bool] = field(default_factory=lambda: [True])
    prepared: list[EntryFill] = field(default_factory=list[EntryFill])
    submitted: list[BrokerOrderCommand] = field(
        default_factory=list[BrokerOrderCommand]
    )
    recovered: list[str] = field(default_factory=list[str])
    confirmed_zero: list[EntryFill] = field(default_factory=list[EntryFill])

    async def prepare_full_close(self, fill: EntryFill) -> BrokerOrderCommand:
        self.prepared.append(fill)
        if self.command is None:
            raise RuntimeError("no emergency command")
        return self.command

    async def submit_locked(self, command: BrokerOrderCommand) -> BrokerWriteResult:
        self.submitted.append(command)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def recover_by_client_id(self, client_order_id: str) -> BrokerWriteResult:
        self.recovered.append(client_order_id)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def confirm_zero_position(self, fill: EntryFill) -> bool:
        self.confirmed_zero.append(fill)
        return self.zero_results.pop(0)


@dataclass
class SafetyActions:
    canceled: list[object] = field(default_factory=list[object])
    halted: list[tuple[object, str]] = field(default_factory=list[tuple[object, str]])

    async def cancel_entry_and_adds(self, binding_id: object) -> None:
        self.canceled.append(binding_id)

    async def halt_account(self, binding_id: object, reason: str) -> None:
        self.halted.append((binding_id, reason))


def emergency_result(command: BrokerOrderCommand) -> BrokerWriteResult:
    return BrokerWriteResult(
        broker_order_id="BINANCE-USDM:912",
        client_order_id=command.broker_client_order_id,
        provider_state="FILLED",
        cumulative_filled_quantity=command.quantity,
        cumulative_quote_quantity=Decimal("121"),
        average_fill_price=Decimal("60500"),
        commissions=(("USDT", Decimal("0.02")),),
        fills=(),
        recovered=False,
    )


def entry_fill(
    *,
    side: Side = Side.BUY,
    cumulative_quantity_before: Decimal = Decimal(),
    tick_size: Decimal = Decimal("0.1"),
) -> EntryFill:
    entry_command_id = new_uuid7()
    return EntryFill(
        entry_command_id=entry_command_id,
        account_id=new_uuid7(),
        instrument_id=new_uuid7(),
        binding_id=new_uuid7(),
        side=side,
        first_fill_quantity=Decimal("0.002"),
        cumulative_quantity_before=cumulative_quantity_before,
        average_fill_price=Decimal("60000"),
        symbol="BTCUSDT",
        tick_size=tick_size,
        filled_at=NOW,
        protection_deadline=NOW + timedelta(seconds=5),
        emergency_close_command_id=new_uuid7(),
    )


def emergency_command(
    fill: EntryFill,
    *,
    quantity: Decimal = Decimal("0.002"),
) -> BrokerOrderCommand:
    emergency_side = Side.SELL if fill.side is Side.BUY else Side.BUY
    return BrokerOrderCommand(
        id=fill.emergency_close_command_id,
        order_id=new_uuid7(),
        account_id=fill.account_id,
        instrument_id=fill.instrument_id,
        command_type=CommandType.SUBMIT,
        target_aggregate_version=2,
        idempotency_key=f"v6-binance-emergency-{fill.entry_command_id.hex}",
        command_sequence=2,
        canonical_payload_hash=b"\x01" * 32,
        broker_client_order_id=binance_normal_client_order_id(
            fill.emergency_close_command_id
        ),
        target_broker_order_id=None,
        replaces_command_id=None,
        origin_type="DAVID_V6_DECISION",
        authority_class="V6_PROVIDER_WRITE",
        owner_runtime_instance_id=new_uuid7(),
        fencing_token=9,
        not_after=NOW + timedelta(minutes=2),
        side=emergency_side,
        order_style=OrderStyle.MARKET,
        quantity=quantity,
        limit_price=None,
        time_in_force="NONE",
        dispatch_attempted_at=NOW,
    )


def risk_authority(
    *,
    side: Side = Side.BUY,
    stop_price: Decimal | None = None,
    allowed: bool = True,
) -> V6RiskAuthority:
    stop = (
        stop_price
        if stop_price is not None
        else (Decimal("59000.09") if side is Side.BUY else Decimal("61000.01"))
    )
    return V6RiskAuthority(
        allowed=allowed,
        blocker_codes=() if allowed else ("BLOCKED",),
        risk_base=Decimal("10000"),
        risk_fraction=Decimal("0.005"),
        risk_budget=Decimal("50"),
        structural_reference=stop,
        stop_price=stop,
        quantity=Decimal("0.002") if allowed else Decimal(),
        stop_distance_atr5m=Decimal("1"),
    )


def algo_response(fill: EntryFill, trigger_price: str) -> BrokerResponse:
    return BrokerResponse(
        status=200,
        body=json.dumps(
            {
                "algoId": 2146760,
                "clientAlgoId": binance_protection_client_algo_id(
                    fill.entry_command_id
                ),
                "algoType": "CONDITIONAL",
                "orderType": "STOP_MARKET",
                "symbol": "BTCUSDT",
                "side": ("SELL" if fill.side is Side.BUY else "BUY"),
                "positionSide": "BOTH",
                "algoStatus": "NEW",
                "triggerPrice": trigger_price,
                "workingType": "MARK_PRICE",
                "closePosition": True,
                "priceProtect": False,
                "reduceOnly": False,
            }
        ).encode(),
    )


def make_service(
    store: MemoryStore,
    sender: Sender,
    emergency: EmergencyOrders,
    safety: SafetyActions,
    *,
    clock: Clock | None = None,
) -> BinanceUsdmProtectionService:
    return BinanceUsdmProtectionService(
        store=store,
        sender=sender,
        emergency_orders=emergency,
        safety_actions=safety,
        clock=Clock() if clock is None else clock,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side", "stop_price", "expected_side", "expected_trigger"),
    [
        (Side.BUY, Decimal("59000.09"), "SELL", "59000"),
        (Side.SELL, Decimal("61000.01"), "BUY", "61000.1"),
    ],
)
async def test_first_fill_persists_exact_close_position_stop_before_network(
    side: Side,
    stop_price: Decimal,
    expected_side: str,
    expected_trigger: str,
) -> None:
    fill = entry_fill(side=side)
    store = MemoryStore()
    sender = Sender(store, [algo_response(fill, expected_trigger)])
    emergency = EmergencyOrders([])
    safety = SafetyActions()

    result = await make_service(store, sender, emergency, safety).protect_first_fill(
        fill, risk_authority(side=side, stop_price=stop_price)
    )

    client_algo_id = binance_protection_client_algo_id(fill.entry_command_id)
    assert client_algo_id == f"v6s-{fill.entry_command_id.hex}"
    assert len(client_algo_id) == 36
    assert store.persisted_before_send is True
    request = sender.calls[0]
    assert request.method == "POST"
    assert request.path == "/fapi/v1/algoOrder"
    parameters = dict(parse_qsl((request.body or b"").decode()))
    assert parameters == {
        "algoType": "CONDITIONAL",
        "symbol": "BTCUSDT",
        "side": expected_side,
        "positionSide": "BOTH",
        "type": "STOP_MARKET",
        "triggerPrice": expected_trigger,
        "workingType": "MARK_PRICE",
        "closePosition": "true",
        "priceProtect": "false",
        "clientAlgoId": client_algo_id,
        "newOrderRespType": "RESULT",
    }
    assert "quantity" not in parameters
    assert "reduceOnly" not in parameters
    assert result.provider_algo_id == "BINANCE-USDM-ALGO:2146760"
    assert result.state is BinanceUsdmAlgoOrderState.ACTIVE
    assert result.recovered is False
    assert emergency.submitted == []
    assert safety.halted == []


@pytest.mark.asyncio
async def test_only_first_nonzero_fill_can_create_initial_protection() -> None:
    fill = entry_fill(cumulative_quantity_before=Decimal("0.001"))
    store = MemoryStore()
    sender = Sender(store, [algo_response(fill, "59000")])

    with pytest.raises(BrokerWriteDisabled, match="first non-zero"):
        await make_service(
            store,
            sender,
            EmergencyOrders([]),
            SafetyActions(),
        ).protect_first_fill(fill, risk_authority())

    assert sender.calls == []
    assert store.rows == {}


@pytest.mark.asyncio
async def test_blocked_risk_authority_cannot_create_protection() -> None:
    fill = entry_fill()
    store = MemoryStore()
    sender = Sender(store, [algo_response(fill, "59000")])

    with pytest.raises(BrokerWriteDisabled, match="risk authority"):
        await make_service(
            store,
            sender,
            EmergencyOrders([]),
            SafetyActions(),
        ).protect_first_fill(fill, risk_authority(allowed=False))

    assert sender.calls == []
    assert store.rows == {}


@pytest.mark.asyncio
async def test_immediate_trigger_rejection_emergency_closes_cancels_and_halts() -> None:
    fill = entry_fill()
    store = MemoryStore()
    sender = Sender(
        store,
        [
            BrokerResponse(
                status=400,
                body=b'{"code":-2021,"msg":"Order would immediately trigger."}',
            )
        ],
    )
    close_command = emergency_command(fill, quantity=Decimal("0.003"))
    emergency_close = emergency_result(close_command)
    emergency = EmergencyOrders([emergency_close], command=close_command)
    safety = SafetyActions()

    with pytest.raises(BinanceUsdmProtectionRejected, match="emergency close"):
        await make_service(store, sender, emergency, safety).protect_first_fill(
            fill, risk_authority()
        )

    client_algo_id = binance_protection_client_algo_id(fill.entry_command_id)
    row = store.rows[client_algo_id]
    assert row.state is BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED
    assert row.result is not None
    assert row.result.emergency_close == emergency_close
    assert emergency.prepared == [fill]
    assert emergency.submitted == [close_command]
    assert emergency.confirmed_zero == [fill]
    assert safety.canceled == [fill.binding_id]
    assert safety.halted == [(fill.binding_id, "BINANCE_PROTECTION_REJECTED")]


@pytest.mark.asyncio
async def test_invalid_provider_acknowledgement_remains_ambiguous() -> None:
    fill = entry_fill()
    store = MemoryStore()
    invalid = algo_response(fill, "59000")
    payload = json.loads(invalid.body)
    payload["clientAlgoId"] = "different"
    sender = Sender(store, [replace(invalid, body=json.dumps(payload).encode())])

    with pytest.raises(RuntimeError, match="unknown"):
        await make_service(
            store,
            sender,
            EmergencyOrders([]),
            SafetyActions(),
        ).protect_first_fill(fill, risk_authority())

    row = store.rows[binance_protection_client_algo_id(fill.entry_command_id)]
    assert row.state is BinanceUsdmAlgoOrderState.AMBIGUOUS
