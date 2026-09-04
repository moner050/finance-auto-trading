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
    BinanceUsdmProtectionNotMoved,
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
from autotrader.risk.v6 import ProtectionAuthority, V6RiskAuthority
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
        origin_type="STRATEGY",
        authority_class="SUBMIT_STRICT_REDUCTION",
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
) -> ProtectionAuthority:
    # Through `of`, so these tests keep exercising the path a real caller
    # takes when it does have the risk engine's own answer.
    stop = (
        stop_price
        if stop_price is not None
        else (Decimal("59000.09") if side is Side.BUY else Decimal("61000.01"))
    )
    return ProtectionAuthority.of(
        V6RiskAuthority(
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


# --- moving a stop, section 31.3 item 3 -----------------------------------
#
# The invariant every one of these is about: a position that has a working
# stop never loses it because the system tried to improve it.


def moved_response(
    fill: EntryFill, placement_command_id: object, trigger_price: str
) -> BrokerResponse:
    return BrokerResponse(
        status=200,
        body=json.dumps(
            {
                "algoId": 2146761,
                "clientAlgoId": binance_protection_client_algo_id(placement_command_id),
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


async def with_working_stop(store: MemoryStore, sender: Sender, fill: EntryFill) -> str:
    """Place the first stop so there is something to supersede."""
    emergency = EmergencyOrders([])
    await make_service(store, sender, emergency, SafetyActions()).protect_first_fill(
        fill, risk_authority(side=fill.side)
    )
    client_algo_id = binance_protection_client_algo_id(fill.entry_command_id)
    assert store.rows[client_algo_id].state is BinanceUsdmAlgoOrderState.ACTIVE
    return client_algo_id


@pytest.mark.asyncio
async def test_move_stop_places_before_it_withdraws() -> None:
    """The order is the design: the position is never without a stop."""
    fill = entry_fill()
    store = MemoryStore()
    placement = new_uuid7()
    sender = Sender(
        store,
        [
            algo_response(fill, "59000"),
            moved_response(fill, placement, "59500"),
            BrokerResponse(status=200, body=b"{}"),
        ],
    )
    first = await with_working_stop(store, sender, fill)

    result = await make_service(
        store, sender, EmergencyOrders([]), SafetyActions()
    ).move_stop(
        fill,
        risk_authority(stop_price=Decimal("59500.04")),
        placement_command_id=placement,
        superseded_client_algo_id=first,
    )

    assert result.state is BinanceUsdmAlgoOrderState.ACTIVE
    assert result.client_algo_id == binance_protection_client_algo_id(placement)
    place, cancel = sender.calls[1], sender.calls[2]
    assert place.method == "POST"
    assert cancel.method == "DELETE"
    assert binance_protection_client_algo_id(placement) in (place.body or b"").decode()
    assert first in cancel.path
    assert store.rows[first].state is BinanceUsdmAlgoOrderState.SUPERSEDED
    assert (
        store.rows[binance_protection_client_algo_id(placement)].state
        is BinanceUsdmAlgoOrderState.ACTIVE
    )


@pytest.mark.asyncio
async def test_a_failed_move_leaves_the_working_stop_alone() -> None:
    """And never closes the position: it is protected, only not improved."""
    fill = entry_fill()
    store = MemoryStore()
    placement = new_uuid7()
    sender = Sender(
        store,
        [algo_response(fill, "59000"), BrokerResponse(status=503, body=b"")],
    )
    first = await with_working_stop(store, sender, fill)
    emergency = EmergencyOrders([])
    safety = SafetyActions()

    with pytest.raises(BinanceUsdmProtectionNotMoved):
        await make_service(store, sender, emergency, safety).move_stop(
            fill,
            risk_authority(stop_price=Decimal("59500.04")),
            placement_command_id=placement,
            superseded_client_algo_id=first,
        )

    assert store.rows[first].state is BinanceUsdmAlgoOrderState.ACTIVE
    assert (
        store.rows[binance_protection_client_algo_id(placement)].state
        is BinanceUsdmAlgoOrderState.AMBIGUOUS
    )
    assert emergency.submitted == []
    assert safety.halted == []
    assert len(sender.calls) == 2


@pytest.mark.asyncio
async def test_a_rejected_move_leaves_the_working_stop_alone() -> None:
    fill = entry_fill()
    store = MemoryStore()
    placement = new_uuid7()
    sender = Sender(
        store,
        [
            algo_response(fill, "59000"),
            BrokerResponse(status=400, body=json.dumps({"code": -4014}).encode()),
        ],
    )
    first = await with_working_stop(store, sender, fill)
    emergency = EmergencyOrders([])

    with pytest.raises(BinanceUsdmProtectionNotMoved):
        await make_service(store, sender, emergency, SafetyActions()).move_stop(
            fill,
            risk_authority(stop_price=Decimal("59500.04")),
            placement_command_id=placement,
            superseded_client_algo_id=first,
        )

    assert store.rows[first].state is BinanceUsdmAlgoOrderState.ACTIVE
    assert (
        store.rows[binance_protection_client_algo_id(placement)].state
        is BinanceUsdmAlgoOrderState.REJECTED
    )
    assert emergency.submitted == []


@pytest.mark.asyncio
async def test_a_failed_withdrawal_keeps_the_new_stop() -> None:
    """Two closePosition stops is over-protected, which is safe. Rolling the
    new one back to keep the count at one would trade that for exposed."""
    fill = entry_fill()
    store = MemoryStore()
    placement = new_uuid7()
    sender = Sender(
        store,
        [
            algo_response(fill, "59000"),
            moved_response(fill, placement, "59500"),
            BrokerResponse(status=503, body=b""),
        ],
    )
    first = await with_working_stop(store, sender, fill)

    result = await make_service(
        store, sender, EmergencyOrders([]), SafetyActions()
    ).move_stop(
        fill,
        risk_authority(stop_price=Decimal("59500.04")),
        placement_command_id=placement,
        superseded_client_algo_id=first,
    )

    assert result.state is BinanceUsdmAlgoOrderState.ACTIVE
    assert store.rows[first].state is BinanceUsdmAlgoOrderState.AMBIGUOUS
    assert (
        store.rows[binance_protection_client_algo_id(placement)].state
        is BinanceUsdmAlgoOrderState.ACTIVE
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [-2011, -2013])
async def test_a_stop_the_venue_no_longer_has_counts_as_withdrawn(code: int) -> None:
    fill = entry_fill()
    store = MemoryStore()
    placement = new_uuid7()
    sender = Sender(
        store,
        [
            algo_response(fill, "59000"),
            moved_response(fill, placement, "59500"),
            BrokerResponse(status=400, body=json.dumps({"code": code}).encode()),
        ],
    )
    first = await with_working_stop(store, sender, fill)

    await make_service(store, sender, EmergencyOrders([]), SafetyActions()).move_stop(
        fill,
        risk_authority(stop_price=Decimal("59500.04")),
        placement_command_id=placement,
        superseded_client_algo_id=first,
    )

    assert store.rows[first].state is BinanceUsdmAlgoOrderState.SUPERSEDED


@pytest.mark.asyncio
async def test_only_an_active_stop_may_be_superseded() -> None:
    """Otherwise this places a second stop behind a position whose protection
    nobody has established."""
    fill = entry_fill()
    store = MemoryStore()
    sender = Sender(store, [algo_response(fill, "59000")])
    first = await with_working_stop(store, sender, fill)
    await store.finish(first, state=BinanceUsdmAlgoOrderState.AMBIGUOUS, result=None)

    with pytest.raises(BinanceUsdmProtectionNotMoved):
        await make_service(
            store, sender, EmergencyOrders([]), SafetyActions()
        ).move_stop(
            fill,
            risk_authority(stop_price=Decimal("59500.04")),
            placement_command_id=new_uuid7(),
            superseded_client_algo_id=first,
        )
    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_a_move_cannot_reuse_the_first_stops_identity() -> None:
    fill = entry_fill()
    store = MemoryStore()
    sender = Sender(store, [algo_response(fill, "59000")])
    first = await with_working_stop(store, sender, fill)

    with pytest.raises(BrokerWriteDisabled):
        await make_service(
            store, sender, EmergencyOrders([]), SafetyActions()
        ).move_stop(
            fill,
            risk_authority(stop_price=Decimal("59500.04")),
            placement_command_id=fill.entry_command_id,
            superseded_client_algo_id=first,
        )
    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_a_stop_belonging_to_another_entry_is_refused() -> None:
    fill = entry_fill()
    stranger = entry_fill()
    store = MemoryStore()
    sender = Sender(store, [algo_response(stranger, "59000")])
    theirs = await with_working_stop(store, sender, stranger)

    with pytest.raises(BrokerWriteDisabled):
        await make_service(
            store, sender, EmergencyOrders([]), SafetyActions()
        ).move_stop(
            fill,
            risk_authority(stop_price=Decimal("59500.04")),
            placement_command_id=new_uuid7(),
            superseded_client_algo_id=theirs,
        )
    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_a_superseded_stop_is_never_recovered() -> None:
    """It was cancelled on purpose. Querying the venue would ask about an order
    this system withdrew, and fail-safing would close a protected position."""
    fill = entry_fill()
    store = MemoryStore()
    placement = new_uuid7()
    sender = Sender(
        store,
        [
            algo_response(fill, "59000"),
            moved_response(fill, placement, "59500"),
            BrokerResponse(status=200, body=b"{}"),
        ],
    )
    first = await with_working_stop(store, sender, fill)
    service = make_service(store, sender, EmergencyOrders([]), SafetyActions())
    await service.move_stop(
        fill,
        risk_authority(stop_price=Decimal("59500.04")),
        placement_command_id=placement,
        superseded_client_algo_id=first,
    )

    with pytest.raises(BinanceUsdmProtectionNotMoved):
        await service.recover_by_client_algo_id(first)
    assert len(sender.calls) == 3
