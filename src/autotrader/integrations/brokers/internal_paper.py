from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import OrderStyle, Side
from autotrader.shared.decimal import require_decimal
from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.models import V6Market

_ACCOUNT_BINDINGS = {
    "internal-krx-paper": V6Market.KRX_CASH,
    "internal-us-paper": V6Market.US_CASH,
    "internal-binance-usdm-paper": V6Market.BINANCE_USDM,
}


class PaperOrderStatus(StrEnum):
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    NO_FILL = "NO_FILL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class PaperExecutionBar:
    bar: CompletedOhlcvBar
    available_quantity: Decimal
    source_digest: bytes

    def __post_init__(self) -> None:
        if type(self.bar) is not CompletedOhlcvBar:
            raise TypeError("bar must be an exact CompletedOhlcvBar")
        quantity = require_decimal(self.available_quantity)
        if quantity < 0:
            raise ValueError("available_quantity must not be negative")
        if type(self.source_digest) is not bytes or len(self.source_digest) != 32:
            raise ValueError("source_digest must be SHA-256 bytes")
        object.__setattr__(self, "available_quantity", quantity)


@dataclass(frozen=True, slots=True)
class PaperOrderCommand:
    id: UUID
    order_id: UUID
    account_alias: str
    market: V6Market
    side: Side
    order_style: OrderStyle
    quantity: Decimal
    limit_price: Decimal | None
    signal_at: datetime
    timeframe: timedelta
    fee_per_unit: Decimal
    slippage_per_unit: Decimal

    def __post_init__(self) -> None:
        _require_uuid7(self.id, "id")
        _require_uuid7(self.order_id, "order_id")
        if _ACCOUNT_BINDINGS.get(self.account_alias) is not self.market:
            raise ValueError("exact internal paper account binding is required")
        if type(self.market) is not V6Market:
            raise TypeError("market must be an exact V6Market")
        if type(self.side) is not Side:
            raise TypeError("side must be an exact Side")
        if type(self.order_style) is not OrderStyle:
            raise TypeError("order_style must be an exact OrderStyle")
        quantity = require_decimal(self.quantity)
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        object.__setattr__(self, "quantity", quantity)
        if self.order_style is OrderStyle.LIMIT:
            if self.limit_price is None:
                raise ValueError("limit order requires limit_price")
            limit_price = require_decimal(self.limit_price)
            if limit_price <= 0:
                raise ValueError("limit_price must be positive")
            object.__setattr__(self, "limit_price", limit_price)
        elif self.limit_price is not None:
            raise ValueError("market order cannot carry limit_price")
        object.__setattr__(self, "signal_at", require_utc(self.signal_at))
        if type(self.timeframe) is not timedelta or self.timeframe <= timedelta(0):
            raise ValueError("timeframe must be positive")
        for name in ("fee_per_unit", "slippage_per_unit"):
            value = require_decimal(getattr(self, name))
            if value < 0:
                raise ValueError(f"{name} must not be negative")
            object.__setattr__(self, name, value)

    def command_digest(self) -> bytes:
        payload = {
            "id": str(self.id),
            "order_id": str(self.order_id),
            "account_alias": self.account_alias,
            "market": self.market.value,
            "side": self.side.value,
            "order_style": self.order_style.value,
            "quantity": _decimal_text(self.quantity),
            "limit_price": (
                None if self.limit_price is None else _decimal_text(self.limit_price)
            ),
            "signal_at": self.signal_at.isoformat(),
            "timeframe_seconds": _decimal_text(
                Decimal(str(self.timeframe.total_seconds()))
            ),
            "fee_per_unit": _decimal_text(self.fee_per_unit),
            "slippage_per_unit": _decimal_text(self.slippage_per_unit),
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).digest()


@dataclass(frozen=True, slots=True)
class PaperOrderReceipt:
    command_id: UUID
    order_id: UUID
    status: PaperOrderStatus
    requested_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    fill_price: Decimal | None
    fee: Decimal
    slippage_cost: Decimal
    filled_at: datetime | None
    reason_code: str | None
    source_digest: bytes | None
    command_digest: bytes


@dataclass(frozen=True, slots=True)
class PaperOrderState:
    order_id: UUID
    status: PaperOrderStatus
    receipt: PaperOrderReceipt | None


class PaperMarketDataPort(Protocol):
    async def next_bar(
        self,
        command: PaperOrderCommand,
    ) -> PaperExecutionBar | None: ...


class PaperJournalPort(Protocol):
    async def load_receipt(
        self,
        command_id: object,
    ) -> PaperOrderReceipt | None: ...

    async def stage_command(
        self,
        command: PaperOrderCommand,
        digest: bytes,
    ) -> None: ...

    async def persist_receipt(self, receipt: PaperOrderReceipt) -> None: ...

    async def latest_receipt_for_order(
        self,
        order_id: object,
    ) -> PaperOrderReceipt | None: ...


class InternalPaperBroker:
    """In-process Paper adapter with no live-provider transport dependency."""

    __slots__ = ("_journal", "_market_data")

    def __init__(
        self,
        *,
        journal: PaperJournalPort,
        market_data: PaperMarketDataPort,
    ) -> None:
        self._journal = journal
        self._market_data = market_data

    async def submit(self, command: PaperOrderCommand) -> PaperOrderReceipt:
        if type(command) is not PaperOrderCommand:
            raise TypeError("command must be an exact PaperOrderCommand")
        command.__post_init__()
        digest = command.command_digest()
        existing = await self._journal.load_receipt(command.id)
        if existing is not None:
            if existing.command_digest != digest:
                raise ValueError("paper command identity payload collision")
            return existing
        await self._journal.stage_command(command, digest)
        execution_bar = await self._market_data.next_bar(command)
        receipt = _fill(command, digest, execution_bar)
        await self._journal.persist_receipt(receipt)
        return receipt

    async def reconcile(self, order_id: UUID) -> PaperOrderState:
        _require_uuid7(order_id, "order_id")
        receipt = await self._journal.latest_receipt_for_order(order_id)
        return PaperOrderState(
            order_id=order_id,
            status=(PaperOrderStatus.UNKNOWN if receipt is None else receipt.status),
            receipt=receipt,
        )


def _fill(
    command: PaperOrderCommand,
    command_digest: bytes,
    execution_bar: PaperExecutionBar | None,
) -> PaperOrderReceipt:
    expected_at = command.signal_at + command.timeframe
    if execution_bar is None or execution_bar.bar.timestamp != expected_at:
        return _no_fill(
            command,
            command_digest,
            reason="MISSING_EXACT_NEXT_BAR",
            source_digest=(
                None if execution_bar is None else execution_bar.source_digest
            ),
        )
    price = _eligible_price(command, execution_bar.bar)
    if price is None:
        return _no_fill(
            command,
            command_digest,
            reason="LIMIT_NOT_REACHED",
            source_digest=execution_bar.source_digest,
        )
    filled_quantity = min(command.quantity, execution_bar.available_quantity)
    if filled_quantity == 0:
        return _no_fill(
            command,
            command_digest,
            reason="INSUFFICIENT_NEXT_BAR_LIQUIDITY",
            source_digest=execution_bar.source_digest,
        )
    remaining = command.quantity - filled_quantity
    status = (
        PaperOrderStatus.FILLED if remaining == 0 else PaperOrderStatus.PARTIALLY_FILLED
    )
    return PaperOrderReceipt(
        command_id=command.id,
        order_id=command.order_id,
        status=status,
        requested_quantity=command.quantity,
        filled_quantity=filled_quantity,
        remaining_quantity=remaining,
        fill_price=price,
        fee=command.fee_per_unit * filled_quantity,
        slippage_cost=command.slippage_per_unit * filled_quantity,
        filled_at=execution_bar.bar.timestamp,
        reason_code=None,
        source_digest=execution_bar.source_digest,
        command_digest=command_digest,
    )


def _eligible_price(
    command: PaperOrderCommand,
    bar: CompletedOhlcvBar,
) -> Decimal | None:
    if command.order_style is OrderStyle.MARKET:
        return (
            bar.open + command.slippage_per_unit
            if command.side is Side.BUY
            else bar.open - command.slippage_per_unit
        )
    assert command.limit_price is not None
    if command.side is Side.BUY:
        if bar.low > command.limit_price:
            return None
        base = min(bar.open, command.limit_price)
        return min(base + command.slippage_per_unit, command.limit_price)
    if bar.high < command.limit_price:
        return None
    base = max(bar.open, command.limit_price)
    return max(base - command.slippage_per_unit, command.limit_price)


def _no_fill(
    command: PaperOrderCommand,
    command_digest: bytes,
    *,
    reason: str,
    source_digest: bytes | None,
) -> PaperOrderReceipt:
    return PaperOrderReceipt(
        command_id=command.id,
        order_id=command.order_id,
        status=PaperOrderStatus.NO_FILL,
        requested_quantity=command.quantity,
        filled_quantity=Decimal(0),
        remaining_quantity=command.quantity,
        fill_price=None,
        fee=Decimal(0),
        slippage_cost=Decimal(0),
        filled_at=None,
        reason_code=reason,
        source_digest=source_digest,
        command_digest=command_digest,
    )


def _require_uuid7(value: object, name: str) -> UUID:
    if not isinstance(value, UUID) or value.version != 7:
        raise ValueError(f"{name} must be UUIDv7")
    return value


def _decimal_text(value: Decimal) -> str:
    return "0" if value.is_zero() else format(value.normalize(), "f")


__all__ = (
    "InternalPaperBroker",
    "PaperExecutionBar",
    "PaperJournalPort",
    "PaperMarketDataPort",
    "PaperOrderCommand",
    "PaperOrderReceipt",
    "PaperOrderState",
    "PaperOrderStatus",
)
