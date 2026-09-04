"""The bridge between dispatch and the venue.

Two things decide everything here and both are checked below: the routing
question (does the command carry a trigger price?) and the refusals, which
are answers rather than gaps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from autotrader.domain.broker_errors import BrokerSubmissionRejected
from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.binance_usdm.algo_orders import (
    BinanceUsdmAlgoOrderState,
    BinanceUsdmProtectionRejected,
    EntryFill,
    ProtectionResult,
    binance_protection_client_algo_id,
)
from autotrader.integrations.brokers.binance_usdm.live_submitter import (
    LiveBrokerSubmitter,
    LiveSubmissionUnsupported,
    ProtectionPlacement,
    ProtectionRejected,
)
from autotrader.integrations.brokers.binance_usdm.orders import (
    BinanceUsdmFill,
    BrokerWriteResult,
    binance_normal_client_order_id,
)
from autotrader.risk.v6 import V6RiskAuthority
from autotrader.shared.ids import new_uuid7

NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


def entry_fill(side: Side = Side.BUY) -> EntryFill:
    return EntryFill(
        entry_command_id=new_uuid7(),
        account_id=new_uuid7(),
        instrument_id=new_uuid7(),
        binding_id=new_uuid7(),
        side=side,
        first_fill_quantity=Decimal("0.002"),
        cumulative_quantity_before=Decimal(),
        average_fill_price=Decimal("60000"),
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        filled_at=NOW,
        protection_deadline=NOW + timedelta(seconds=5),
        emergency_close_command_id=new_uuid7(),
    )


def authority(side: Side = Side.BUY) -> V6RiskAuthority:
    return V6RiskAuthority(
        allowed=True,
        blocker_codes=(),
        risk_base=Decimal("1000"),
        risk_fraction=Decimal("0.005"),
        risk_budget=Decimal("5"),
        structural_reference=Decimal("59000") if side is Side.BUY else Decimal("61000"),
        stop_price=Decimal("59000") if side is Side.BUY else Decimal("61000"),
        quantity=Decimal("0.002"),
        stop_distance_atr5m=Decimal("1.0"),
    )


def command(
    *,
    command_type: CommandType = CommandType.SUBMIT,
    trigger_price: Decimal | None = None,
    identity: UUID | None = None,
) -> BrokerOrderCommand:
    command_id = identity or new_uuid7()
    return BrokerOrderCommand(
        id=command_id,
        order_id=new_uuid7(),
        account_id=new_uuid7(),
        instrument_id=new_uuid7(),
        command_type=command_type,
        target_aggregate_version=2,
        idempotency_key=f"v6-binance-{command_id.hex}",
        command_sequence=2,
        canonical_payload_hash=b"\x01" * 32,
        broker_client_order_id=binance_normal_client_order_id(command_id),
        target_broker_order_id=None,
        replaces_command_id=None,
        origin_type="DAVID_V6_DECISION",
        authority_class="V6_PROVIDER_WRITE",
        owner_runtime_instance_id=new_uuid7(),
        fencing_token=9,
        not_after=NOW + timedelta(minutes=2),
        side=Side.BUY,
        order_style=OrderStyle.MARKET,
        quantity=Decimal("0.002"),
        limit_price=None,
        time_in_force="NONE",
        trigger_price=trigger_price,
        dispatch_attempted_at=NOW,
    )


def write_result() -> BrokerWriteResult:
    return BrokerWriteResult(
        broker_order_id="BINANCE-USDM:8389765812345678901",
        client_order_id=f"v6-{new_uuid7().hex}",
        provider_state="FILLED",
        cumulative_filled_quantity=Decimal("0.002"),
        cumulative_quote_quantity=Decimal("120"),
        average_fill_price=Decimal("60000"),
        commissions=(("USDT", Decimal("0.048")),),
        fills=(
            BinanceUsdmFill(
                trade_id=1,
                order_id=8389765812345678901,
                side=Side.BUY,
                quantity=Decimal("0.002"),
                price=Decimal("60000"),
                commission=Decimal("0.048"),
                commission_asset="USDT",
                realized_pnl=Decimal(0),
                occurred_at=NOW,
            ),
        ),
        recovered=False,
    )


def protection_result(
    client_algo_id: str,
    *,
    provider_algo_id: str | None = "BINANCE-USDM-ALGO:2146760",
    state: BinanceUsdmAlgoOrderState = BinanceUsdmAlgoOrderState.ACTIVE,
    emergency: BrokerWriteResult | None = None,
) -> ProtectionResult:
    return ProtectionResult(
        provider_algo_id=provider_algo_id,
        client_algo_id=client_algo_id,
        state=state,
        trigger_price=Decimal("59000"),
        recovered=False,
        emergency_close=emergency,
    )


@dataclass
class Orders:
    result: BrokerWriteResult | BaseException
    submitted: list[BrokerOrderCommand] = field(
        default_factory=list[BrokerOrderCommand]
    )
    recovered: list[str] = field(default_factory=list[str])

    async def submit_locked(self, command: BrokerOrderCommand) -> BrokerWriteResult:
        self.submitted.append(command)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    async def recover_by_client_id(self, client_order_id: str) -> BrokerWriteResult:
        self.recovered.append(client_order_id)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@dataclass
class Protection:
    result: ProtectionResult | BaseException
    first: list[EntryFill] = field(default_factory=list[EntryFill])
    moves: list[tuple[object, str]] = field(default_factory=list[tuple[object, str]])
    recovered: list[str] = field(default_factory=list[str])

    def _answer(self) -> ProtectionResult:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    async def protect_first_fill(
        self, fill: EntryFill, authority: V6RiskAuthority
    ) -> ProtectionResult:
        del authority
        self.first.append(fill)
        return self._answer()

    async def move_stop(
        self,
        fill: EntryFill,
        authority: V6RiskAuthority,
        *,
        placement_command_id: object,
        superseded_client_algo_id: str,
    ) -> ProtectionResult:
        del fill, authority
        self.moves.append((placement_command_id, superseded_client_algo_id))
        return self._answer()

    async def recover_by_client_algo_id(self, client_algo_id: str) -> ProtectionResult:
        self.recovered.append(client_algo_id)
        return self._answer()


@dataclass
class Context:
    placement: ProtectionPlacement
    asked: list[BrokerOrderCommand] = field(default_factory=list[BrokerOrderCommand])

    async def placement_for(self, command: BrokerOrderCommand) -> ProtectionPlacement:
        self.asked.append(command)
        return self.placement


def submitter(
    orders: Orders, protection: Protection, context: Context
) -> LiveBrokerSubmitter:
    return LiveBrokerSubmitter(orders, protection, context)  # pyright: ignore[reportArgumentType]


@pytest.mark.asyncio
async def test_an_order_without_a_trigger_goes_to_the_ordinary_path() -> None:
    fill = entry_fill()
    orders = Orders(write_result())
    protection = Protection(protection_result("v6s-x"))
    context = Context(ProtectionPlacement(fill, authority()))
    sent = command()

    receipt = await submitter(orders, protection, context).submit(sent)

    assert receipt.broker_order_id == "BINANCE-USDM:8389765812345678901"
    assert orders.submitted == [sent]
    assert protection.first == []
    # The context is not even consulted: nothing about a market order needs it.
    assert context.asked == []


@pytest.mark.asyncio
async def test_a_trigger_price_routes_to_protection() -> None:
    fill = entry_fill()
    client_algo_id = binance_protection_client_algo_id(fill.entry_command_id)
    orders = Orders(write_result())
    protection = Protection(protection_result(client_algo_id))
    context = Context(ProtectionPlacement(fill, authority()))

    receipt = await submitter(orders, protection, context).submit(
        command(trigger_price=Decimal("59000"))
    )

    assert receipt.broker_order_id == "BINANCE-USDM-ALGO:2146760"
    assert protection.first == [fill]
    assert orders.submitted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command_type", [CommandType.SUBMIT, CommandType.REPLACE])
async def test_a_working_stop_makes_it_a_move_whatever_the_command_says(
    command_type: CommandType,
) -> None:
    """Which one it is comes from the placement, not the command type."""
    fill = entry_fill()
    superseded = binance_protection_client_algo_id(fill.entry_command_id)
    identity = new_uuid7()
    orders = Orders(write_result())
    protection = Protection(
        protection_result(binance_protection_client_algo_id(identity))
    )
    context = Context(ProtectionPlacement(fill, authority(), superseded))
    sent = command(
        command_type=command_type,
        trigger_price=Decimal("59500"),
        identity=identity,
    )

    live = submitter(orders, protection, context)
    if command_type is CommandType.SUBMIT:
        await live.submit(sent)
    else:
        await live.replace(sent)

    assert protection.first == []
    assert protection.moves == [(identity, superseded)]


@pytest.mark.asyncio
async def test_a_market_order_has_nothing_to_cancel_or_replace() -> None:
    live = submitter(
        Orders(write_result()),
        Protection(protection_result("v6s-x")),
        Context(ProtectionPlacement(entry_fill(), authority())),
    )
    with pytest.raises(LiveSubmissionUnsupported):
        await live.cancel(command(command_type=CommandType.CANCEL))
    with pytest.raises(LiveSubmissionUnsupported):
        await live.replace(command(command_type=CommandType.REPLACE))


@pytest.mark.asyncio
async def test_a_stop_is_never_withdrawn_on_its_own() -> None:
    """A bare cancel would leave the position unprotected, which is the one
    state this system may not enter on purpose."""
    live = submitter(
        Orders(write_result()),
        Protection(protection_result("v6s-x")),
        Context(ProtectionPlacement(entry_fill(), authority())),
    )
    with pytest.raises(LiveSubmissionUnsupported):
        await live.cancel(
            command(command_type=CommandType.CANCEL, trigger_price=Decimal("59000"))
        )


@pytest.mark.asyncio
async def test_an_authoritative_refusal_reaches_dispatch_as_a_rejection() -> None:
    """`BinanceUsdmProtectionRejected` is a plain RuntimeError, so dispatch
    would file the venue's answer as UNKNOWN and keep retrying it."""
    fill = entry_fill()
    protection = Protection(BinanceUsdmProtectionRejected("refused"))
    live = submitter(
        Orders(write_result()),
        protection,
        Context(ProtectionPlacement(fill, authority())),
    )
    with pytest.raises(ProtectionRejected) as caught:
        await live.submit(command(trigger_price=Decimal("59000")))
    assert isinstance(caught.value, BrokerSubmissionRejected)


@pytest.mark.asyncio
async def test_an_emergency_close_is_not_reported_as_a_working_stop() -> None:
    """The position was flattened, so there is no order for dispatch to
    record. A receipt here would file a stop that does not exist."""
    fill = entry_fill()
    client_algo_id = binance_protection_client_algo_id(fill.entry_command_id)
    protection = Protection(
        protection_result(
            client_algo_id,
            provider_algo_id=None,
            state=BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED,
            emergency=write_result(),
        )
    )
    live = submitter(
        Orders(write_result()),
        protection,
        Context(ProtectionPlacement(fill, authority())),
    )
    with pytest.raises(LiveSubmissionUnsupported):
        await live.submit(command(trigger_price=Decimal("59000")))


@pytest.mark.asyncio
async def test_recovery_asks_the_path_the_command_belongs_to() -> None:
    fill = entry_fill()
    first = binance_protection_client_algo_id(fill.entry_command_id)
    orders = Orders(write_result())
    protection = Protection(protection_result(first))
    context = Context(ProtectionPlacement(fill, authority()))
    live = submitter(orders, protection, context)

    plain = command()
    assert await live.recover_submit(plain, now=NOW) is not None
    assert orders.recovered == [plain.broker_client_order_id]
    assert protection.recovered == []

    await live.recover_submit(command(trigger_price=Decimal("59000")), now=NOW)
    assert protection.recovered == [first]


@pytest.mark.asyncio
async def test_recovery_of_a_move_asks_for_the_moves_own_identity() -> None:
    fill = entry_fill()
    identity = new_uuid7()
    moved = binance_protection_client_algo_id(identity)
    protection = Protection(protection_result(moved))
    context = Context(
        ProtectionPlacement(
            fill,
            authority(),
            binance_protection_client_algo_id(fill.entry_command_id),
        )
    )
    live = submitter(Orders(write_result()), protection, context)

    await live.recover_submit(
        command(trigger_price=Decimal("59500"), identity=identity), now=NOW
    )
    assert protection.recovered == [moved]


@pytest.mark.asyncio
async def test_each_entry_point_insists_on_its_own_command_type() -> None:
    live = submitter(
        Orders(write_result()),
        Protection(protection_result("v6s-x")),
        Context(ProtectionPlacement(entry_fill(), authority())),
    )
    with pytest.raises(ValueError):
        await live.submit(command(command_type=CommandType.CANCEL))
    with pytest.raises(ValueError):
        await live.cancel(command(command_type=CommandType.SUBMIT))
    with pytest.raises(ValueError):
        await live.replace(command(command_type=CommandType.SUBMIT))
