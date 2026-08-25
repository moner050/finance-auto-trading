from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
from typing import Protocol, cast
from urllib.parse import parse_qsl, urlencode
from uuid import UUID

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.common import (
    BrokerRequest,
    BrokerResponse,
    BrokerSubmissionRejected,
    BrokerWriteDisabled,
)

_SYMBOL = "BTCUSDT"
_STRATEGY_VERSION = "david-trullas-v6.0"
_AUTHORITY_MAX_AGE = timedelta(seconds=30)
_ALLOWED_ORDER_STATUSES = frozenset(
    {
        "NEW",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "REJECTED",
        "EXPIRED",
        "EXPIRED_IN_MATCH",
    }
)
_AMBIGUOUS_POST_ERROR_CODES = frozenset({-1000, -1006, -1007, -4116})
_AUTHORITATIVE_POST_ERROR_CODES = frozenset({-1008})
_AUTHORITATIVE_POST_ERROR_RANGES = (
    (-1999, -1100),
    (-2999, -2000),
    (-4999, -4000),
)


class BinanceUsdmOrderUnknown(RuntimeError):
    """The normal order may exist and must never be blindly submitted again."""


class BinanceUsdmOrderNotSent(RuntimeError):
    """Durable transport evidence proves no request bytes were sent."""


class BinanceUsdmOrderRejected(BrokerSubmissionRejected):
    """Binance authoritatively rejected the normal order."""


class BinanceUsdmPreSendFailure(RuntimeError):
    """A transport failure that proves no request bytes left the process."""


class BinanceUsdmOrderRole(StrEnum):
    ENTRY = "ENTRY"
    ADD = "ADD"
    SESSION_CLOSE = "SESSION_CLOSE"
    RISK_CLOSE = "RISK_CLOSE"
    EMERGENCY_CLOSE = "EMERGENCY_CLOSE"

    @property
    def opens_exposure(self) -> bool:
        return self in {self.ENTRY, self.ADD}


class BinanceUsdmNormalOrderState(StrEnum):
    PREPARED = "PREPARED"
    NOT_SENT = "NOT_SENT"
    AMBIGUOUS = "AMBIGUOUS"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class BinanceUsdmSymbolFilters:
    tick_size: Decimal
    step_size: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal
    captured_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "tick_size",
            "step_size",
            "minimum_quantity",
            "minimum_notional",
        ):
            value = _decimal(getattr(self, name), name)
            if value <= 0:
                raise ValueError(f"Binance USD-M {name} must be positive")
        _utc(self.captured_at, "symbol filter captured_at")


@dataclass(frozen=True, slots=True)
class BinanceUsdmNormalOrderAuthority:
    command_id: UUID
    account_id: UUID
    instrument_id: UUID
    binding_id: UUID
    binding_generation: int
    policy_version_id: UUID
    strategy_version: str
    writer_capability: bool
    account_enabled: bool
    binding_active: bool
    intent_locked: bool
    symbol: str
    role: BinanceUsdmOrderRole
    side: Side
    order_style: OrderStyle
    quantity: Decimal
    limit_price: Decimal | None
    expected_leverage: int
    verified_leverage: int
    leverage_verified_at: datetime
    position_mode: str
    margin_type: str
    auto_add_margin: bool
    filters: BinanceUsdmSymbolFilters
    notional_reference_price: Decimal
    authorized_reduce_quantity: Decimal


@dataclass(frozen=True, slots=True)
class BinanceUsdmFill:
    trade_id: int
    order_id: int
    side: Side
    quantity: Decimal
    price: Decimal
    commission: Decimal
    commission_asset: str
    realized_pnl: Decimal
    occurred_at: datetime

    def __post_init__(self) -> None:
        if type(self.trade_id) is not int or self.trade_id < 0:
            raise ValueError("Binance USD-M trade ID is invalid")
        if type(self.order_id) is not int or self.order_id <= 0:
            raise ValueError("Binance USD-M fill order ID is invalid")
        if type(self.side) is not Side:
            raise TypeError("Binance USD-M fill side must be exact")
        for name in ("quantity", "price"):
            if _decimal(getattr(self, name), name) <= 0:
                raise ValueError(f"Binance USD-M fill {name} must be positive")
        if _decimal(self.commission, "commission") < 0:
            raise ValueError("Binance USD-M commission cannot be negative")
        _decimal(self.realized_pnl, "realized_pnl")
        _asset(self.commission_asset)
        _utc_instant(self.occurred_at, "fill occurred_at")


@dataclass(frozen=True, slots=True)
class BrokerWriteResult:
    broker_order_id: str
    client_order_id: str
    provider_state: str
    cumulative_filled_quantity: Decimal
    cumulative_quote_quantity: Decimal
    average_fill_price: Decimal
    commissions: tuple[tuple[str, Decimal], ...]
    fills: tuple[BinanceUsdmFill, ...]
    recovered: bool

    def __post_init__(self) -> None:
        _provider_order_id(self.broker_order_id)
        _client_order_id(self.client_order_id)
        if self.provider_state not in _ALLOWED_ORDER_STATUSES:
            raise ValueError("Binance USD-M provider state is invalid")
        for name in (
            "cumulative_filled_quantity",
            "cumulative_quote_quantity",
            "average_fill_price",
        ):
            if _decimal(getattr(self, name), name) < 0:
                raise ValueError(f"Binance USD-M {name} cannot be negative")
        if type(self.commissions) is not tuple:
            raise TypeError("Binance USD-M commissions must be a tuple")
        for asset, amount in self.commissions:
            _asset(asset)
            if _decimal(amount, "commission amount") < 0:
                raise ValueError("Binance USD-M commission cannot be negative")
        if type(self.fills) is not tuple or any(
            type(fill) is not BinanceUsdmFill for fill in self.fills
        ):
            raise TypeError("Binance USD-M fills must contain exact values")
        if type(self.recovered) is not bool:
            raise TypeError("Binance USD-M recovered must be bool")


@dataclass(frozen=True, slots=True)
class BinanceUsdmNormalOrderRecord:
    command_id: UUID
    account_id: UUID
    binding_id: UUID
    client_order_id: str
    request_body: bytes
    request_digest: bytes
    prepared_at: datetime
    not_after: datetime
    dispatch_count: int
    state: BinanceUsdmNormalOrderState
    result: BrokerWriteResult | None

    @classmethod
    def prepared(
        cls,
        *,
        command: BrokerOrderCommand,
        authority: BinanceUsdmNormalOrderAuthority,
        request: BrokerRequest,
    ) -> BinanceUsdmNormalOrderRecord:
        attempted_at = _utc(command.dispatch_attempted_at, "dispatch_attempted_at")
        if request.method != "POST" or request.path != "/fapi/v1/order":
            raise ValueError("Binance USD-M canonical order request is invalid")
        body = request.body
        if type(body) is not bytes or not body:
            raise ValueError("Binance USD-M canonical order body is invalid")
        result = cls(
            command_id=command.id,
            account_id=command.account_id,
            binding_id=authority.binding_id,
            client_order_id=command.broker_client_order_id,
            request_body=body,
            request_digest=sha256(body).digest(),
            prepared_at=attempted_at,
            not_after=command.not_after,
            dispatch_count=1,
            state=BinanceUsdmNormalOrderState.PREPARED,
            result=None,
        )
        result.validate()
        return result

    def validate(self) -> None:
        for name in ("command_id", "account_id", "binding_id"):
            _uuid7(getattr(self, name), name)
        _client_order_id(self.client_order_id)
        if type(self.request_body) is not bytes or not self.request_body:
            raise ValueError("Binance USD-M persisted request body is invalid")
        if (
            type(self.request_digest) is not bytes
            or len(self.request_digest) != 32
            or sha256(self.request_body).digest() != self.request_digest
        ):
            raise ValueError("Binance USD-M persisted request digest is invalid")
        prepared_at = _utc(self.prepared_at, "prepared_at")
        not_after = _utc(self.not_after, "not_after")
        if prepared_at >= not_after:
            raise ValueError("Binance USD-M persisted request window is invalid")
        if type(self.dispatch_count) is not int or self.dispatch_count <= 0:
            raise ValueError("Binance USD-M dispatch count is invalid")
        if type(self.state) is not BinanceUsdmNormalOrderState:
            raise TypeError("Binance USD-M order state must be exact")
        if self.state is BinanceUsdmNormalOrderState.ACKNOWLEDGED:
            if type(self.result) is not BrokerWriteResult:
                raise ValueError("acknowledged Binance USD-M order needs a result")
        elif self.result is not None:
            raise ValueError("only acknowledged Binance USD-M order has a result")


@dataclass(frozen=True, slots=True)
class BinanceUsdmNormalOrderClaim:
    record: BinanceUsdmNormalOrderRecord
    acquired: bool

    def __post_init__(self) -> None:
        if type(self.record) is not BinanceUsdmNormalOrderRecord:
            raise TypeError("Binance USD-M normal order record must be exact")
        self.record.validate()
        if type(self.acquired) is not bool:
            raise TypeError("Binance USD-M normal order claim must be bool")
        if (
            self.acquired
            and self.record.state is not BinanceUsdmNormalOrderState.PREPARED
        ):
            raise ValueError("only a prepared Binance USD-M order may be acquired")


class BinanceUsdmOrderAuthoritySource(Protocol):
    async def load(
        self, command: BrokerOrderCommand
    ) -> BinanceUsdmNormalOrderAuthority: ...


class BinanceUsdmOrderSender(Protocol):
    async def send(self, request: BrokerRequest) -> BrokerResponse: ...


class BinanceUsdmNormalOrderStore(Protocol):
    async def prepare(
        self, record: BinanceUsdmNormalOrderRecord
    ) -> BinanceUsdmNormalOrderClaim: ...

    async def load_by_client_id(
        self, client_order_id: str
    ) -> BinanceUsdmNormalOrderRecord | None: ...

    async def mark_not_sent(
        self, client_order_id: str, *, request_digest: bytes
    ) -> BinanceUsdmNormalOrderRecord: ...

    async def finish(
        self,
        client_order_id: str,
        *,
        state: BinanceUsdmNormalOrderState,
        result: object | None,
    ) -> BinanceUsdmNormalOrderRecord: ...


@dataclass(frozen=True, slots=True)
class _ProviderOrder:
    order_id: int
    client_order_id: str
    symbol: str
    status: str
    side: Side
    order_type: OrderStyle
    original_quantity: Decimal
    executed_quantity: Decimal
    cumulative_quote: Decimal
    average_price: Decimal
    reduce_only: bool
    position_side: str


class BinanceUsdmOrderService:
    def __init__(
        self,
        *,
        authority: BinanceUsdmOrderAuthoritySource,
        store: BinanceUsdmNormalOrderStore,
        sender: BinanceUsdmOrderSender,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(clock):
            raise TypeError("Binance USD-M clock must be callable")
        self._authority = authority
        self._store = store
        self._sender = sender
        self._clock = clock

    async def submit_locked(self, command: BrokerOrderCommand) -> BrokerWriteResult:
        now = _utc(self._clock(), "order service clock")
        _validate_recovery_command(command)
        existing = await self._store.load_by_client_id(command.broker_client_order_id)
        if existing is not None:
            if type(existing) is not BinanceUsdmNormalOrderRecord:
                raise BinanceUsdmOrderUnknown(
                    "Binance USD-M durable order record is invalid"
                )
            _validate_persisted_command(command, existing)
            if existing.state is not BinanceUsdmNormalOrderState.NOT_SENT:
                return await self._existing_or_recover(existing)
        authority = await self._load_authority(command, now=now)
        request = build_binance_usdm_order_request(command, authority)
        digest = sha256(request.body or b"").digest()
        if command.canonical_payload_hash != digest:
            raise BrokerWriteDisabled(
                "Binance USD-M canonical payload hash does not match"
            )
        prepared = BinanceUsdmNormalOrderRecord.prepared(
            command=command,
            authority=authority,
            request=request,
        )
        claim = await self._store.prepare(prepared)
        if type(claim) is not BinanceUsdmNormalOrderClaim:
            raise BinanceUsdmOrderUnknown(
                "Binance USD-M durable order claim is invalid"
            )
        record = claim.record
        if not claim.acquired:
            return await self._existing_or_recover(record)
        try:
            response = await self._sender.send(request)
        except BinanceUsdmPreSendFailure:
            await self._store.mark_not_sent(
                record.client_order_id,
                request_digest=record.request_digest,
            )
            raise BinanceUsdmOrderNotSent(
                "Binance USD-M order request was not sent"
            ) from None
        except asyncio.CancelledError, KeyboardInterrupt, SystemExit:
            await self._mark_ambiguous(record.client_order_id)
            raise
        except Exception:
            await self._mark_ambiguous(record.client_order_id)
            raise BinanceUsdmOrderUnknown(
                "Binance USD-M order dispatch outcome is unknown"
            ) from None
        return await self._accept_post_response(record, response)

    async def recover_by_client_id(self, client_order_id: str) -> BrokerWriteResult:
        _client_order_id(client_order_id)
        record = await self._store.load_by_client_id(client_order_id)
        if type(record) is not BinanceUsdmNormalOrderRecord:
            raise BinanceUsdmOrderUnknown(
                "Binance USD-M durable order record is unavailable"
            )
        record.validate()
        if record.state is BinanceUsdmNormalOrderState.ACKNOWLEDGED:
            assert type(record.result) is BrokerWriteResult
            return record.result
        if record.state is BinanceUsdmNormalOrderState.REJECTED:
            raise BinanceUsdmOrderRejected("Binance USD-M order was rejected")
        if record.state is BinanceUsdmNormalOrderState.UNKNOWN:
            raise BinanceUsdmOrderUnknown("Binance USD-M order is terminal unknown")
        if record.state is BinanceUsdmNormalOrderState.NOT_SENT:
            raise BinanceUsdmOrderNotSent("Binance USD-M order request was not sent")
        query = BrokerRequest(
            method="GET",
            path=(
                "/fapi/v1/order?"
                + urlencode(
                    (
                        ("symbol", _SYMBOL),
                        ("origClientOrderId", client_order_id),
                    )
                )
            ),
        )
        try:
            response = await self._sender.send(query)
        except asyncio.CancelledError, KeyboardInterrupt, SystemExit:
            raise
        except Exception:
            raise BinanceUsdmOrderUnknown(
                "Binance USD-M order recovery is unavailable"
            ) from None
        if _is_order_not_found(response):
            raise BinanceUsdmOrderUnknown("Binance USD-M order is not yet found")
        if response.status != 200:
            raise BinanceUsdmOrderUnknown("Binance USD-M order recovery is unavailable")
        try:
            order = _decode_provider_order(response, record)
            result = await self._result_with_fills(order, recovered=True)
        except asyncio.CancelledError, KeyboardInterrupt, SystemExit:
            raise
        except Exception:
            raise BinanceUsdmOrderUnknown(
                "Binance USD-M recovered order evidence is invalid"
            ) from None
        await self._store.finish(
            client_order_id,
            state=BinanceUsdmNormalOrderState.ACKNOWLEDGED,
            result=result,
        )
        return result

    async def _load_authority(
        self,
        command: BrokerOrderCommand,
        *,
        now: datetime,
    ) -> BinanceUsdmNormalOrderAuthority:
        if type(command) is not BrokerOrderCommand:
            raise TypeError("exact BrokerOrderCommand is required")
        authority = await self._authority.load(command)
        if type(authority) is not BinanceUsdmNormalOrderAuthority:
            raise BrokerWriteDisabled("exact Binance USD-M authority is absent")
        _validate_write_authority(command, authority, now=now)
        return authority

    async def _existing_or_recover(
        self, record: BinanceUsdmNormalOrderRecord
    ) -> BrokerWriteResult:
        if record.state is BinanceUsdmNormalOrderState.ACKNOWLEDGED:
            assert type(record.result) is BrokerWriteResult
            return record.result
        if record.state is BinanceUsdmNormalOrderState.REJECTED:
            raise BinanceUsdmOrderRejected("Binance USD-M order was rejected")
        if record.state is BinanceUsdmNormalOrderState.UNKNOWN:
            raise BinanceUsdmOrderUnknown("Binance USD-M order is terminal unknown")
        if record.state is BinanceUsdmNormalOrderState.NOT_SENT:
            raise BinanceUsdmOrderNotSent("Binance USD-M order request was not sent")
        return await self.recover_by_client_id(record.client_order_id)

    async def _accept_post_response(
        self,
        record: BinanceUsdmNormalOrderRecord,
        response: BrokerResponse,
    ) -> BrokerWriteResult:
        if (
            response.status == 408
            or response.status >= 500
            or (
                400 <= response.status < 500
                and not _post_response_is_authoritative_rejection(response)
            )
        ):
            await self._mark_ambiguous(record.client_order_id)
            raise BinanceUsdmOrderUnknown(
                "Binance USD-M order dispatch outcome is unknown"
            )
        if 400 <= response.status < 500:
            await self._store.finish(
                record.client_order_id,
                state=BinanceUsdmNormalOrderState.REJECTED,
                result=None,
            )
            raise BinanceUsdmOrderRejected("Binance USD-M order was rejected")
        if response.status != 200:
            await self._mark_ambiguous(record.client_order_id)
            raise BinanceUsdmOrderUnknown(
                "Binance USD-M order dispatch outcome is unknown"
            )
        try:
            order = _decode_provider_order(response, record)
            result = await self._result_with_fills(order, recovered=False)
        except asyncio.CancelledError, KeyboardInterrupt, SystemExit:
            await self._mark_ambiguous(record.client_order_id)
            raise
        except Exception:
            await self._mark_ambiguous(record.client_order_id)
            raise BinanceUsdmOrderUnknown(
                "Binance USD-M order acknowledgement is invalid"
            ) from None
        await self._store.finish(
            record.client_order_id,
            state=BinanceUsdmNormalOrderState.ACKNOWLEDGED,
            result=result,
        )
        return result

    async def _result_with_fills(
        self,
        order: _ProviderOrder,
        *,
        recovered: bool,
    ) -> BrokerWriteResult:
        response = await self._sender.send(
            BrokerRequest(
                method="GET",
                path=(
                    "/fapi/v1/userTrades?"
                    + urlencode(
                        (
                            ("symbol", _SYMBOL),
                            ("orderId", str(order.order_id)),
                            ("limit", "1000"),
                        )
                    )
                ),
            )
        )
        if response.status != 200:
            raise ValueError("Binance USD-M fills are unavailable")
        fills = _decode_fills(response, order)
        _match_cumulative_order_facts(order, fills)
        commissions: dict[str, Decimal] = {}
        for fill in fills:
            commissions[fill.commission_asset] = (
                commissions.get(fill.commission_asset, Decimal()) + fill.commission
            )
        return BrokerWriteResult(
            broker_order_id=f"BINANCE-USDM:{order.order_id}",
            client_order_id=order.client_order_id,
            provider_state=order.status,
            cumulative_filled_quantity=order.executed_quantity,
            cumulative_quote_quantity=order.cumulative_quote,
            average_fill_price=order.average_price,
            commissions=tuple(sorted(commissions.items())),
            fills=fills,
            recovered=recovered,
        )

    async def _mark_ambiguous(self, client_order_id: str) -> None:
        await self._store.finish(
            client_order_id,
            state=BinanceUsdmNormalOrderState.AMBIGUOUS,
            result=None,
        )


def binance_normal_client_order_id(command_id: UUID) -> str:
    _uuid7(command_id, "command_id")
    return f"v6-{command_id.hex}"


def build_binance_usdm_order_request(
    command: BrokerOrderCommand,
    authority: BinanceUsdmNormalOrderAuthority,
) -> BrokerRequest:
    if type(command) is not BrokerOrderCommand:
        raise TypeError("exact BrokerOrderCommand is required")
    if type(authority) is not BinanceUsdmNormalOrderAuthority:
        raise TypeError("exact Binance USD-M order authority is required")
    parameters: list[tuple[str, str]] = [
        ("symbol", authority.symbol),
        ("side", command.side.value),
        ("positionSide", "BOTH"),
        ("type", command.order_style.value),
        ("quantity", _decimal_text(command.quantity)),
    ]
    if command.order_style is OrderStyle.LIMIT:
        parameters.extend(
            (
                ("price", _decimal_text(command.limit_price)),
                ("timeInForce", command.time_in_force),
            )
        )
    if not authority.role.opens_exposure:
        parameters.append(("reduceOnly", "true"))
    parameters.extend(
        (
            ("newClientOrderId", command.broker_client_order_id),
            ("newOrderRespType", "RESULT"),
        )
    )
    return BrokerRequest(
        method="POST",
        path="/fapi/v1/order",
        headers=(("Content-Type", "application/x-www-form-urlencoded"),),
        body=urlencode(parameters).encode("ascii"),
    )


def _validate_write_authority(
    command: BrokerOrderCommand,
    authority: BinanceUsdmNormalOrderAuthority,
    *,
    now: datetime,
) -> None:
    try:
        for name in (
            "command_id",
            "account_id",
            "instrument_id",
            "binding_id",
            "policy_version_id",
        ):
            _uuid7(getattr(authority, name), name)
        if (
            command.command_type is not CommandType.SUBMIT
            or command.target_broker_order_id is not None
            or command.replaces_command_id is not None
            or command.origin_type != "DAVID_V6_DECISION"
            or command.authority_class != "V6_PROVIDER_WRITE"
            or authority.command_id != command.id
            or authority.account_id != command.account_id
            or authority.instrument_id != command.instrument_id
            or type(authority.binding_generation) is not int
            or authority.binding_generation <= 0
            or command.fencing_token != authority.binding_generation
            or authority.strategy_version != _STRATEGY_VERSION
            or not authority.writer_capability
            or not authority.account_enabled
            or not authority.binding_active
            or not authority.intent_locked
        ):
            raise BrokerWriteDisabled(
                "exact Binance USD-M write authority does not match"
            )
        if authority.symbol != _SYMBOL:
            raise BrokerWriteDisabled("Binance USD-M supports BTCUSDT only")
        if type(authority.role) is not BinanceUsdmOrderRole:
            raise BrokerWriteDisabled("Binance USD-M order role is invalid")
        if (
            type(authority.side) is not Side
            or type(authority.order_style) is not OrderStyle
            or command.side is not authority.side
            or command.order_style is not authority.order_style
            or command.quantity != authority.quantity
            or command.limit_price != authority.limit_price
        ):
            raise BrokerWriteDisabled("Binance USD-M locked intent does not match")
        if command.broker_client_order_id != binance_normal_client_order_id(command.id):
            raise BrokerWriteDisabled(
                "Binance USD-M deterministic client ID does not match"
            )
        attempted_at = _utc(command.dispatch_attempted_at, "dispatch_attempted_at")
        not_after = _utc(command.not_after, "not_after")
        if not attempted_at <= now < not_after:
            raise BrokerWriteDisabled("Binance USD-M command window is invalid")
        if type(authority.filters) is not BinanceUsdmSymbolFilters:
            raise BrokerWriteDisabled(
                "Binance USD-M exchange filter authority is absent"
            )
        authority.filters.__post_init__()
        if not _fresh(authority.filters.captured_at, now):
            raise BrokerWriteDisabled(
                "Binance USD-M exchange filter authority is stale"
            )
        quantity = _decimal(command.quantity, "quantity")
        if quantity <= 0 or quantity < authority.filters.minimum_quantity:
            raise BrokerWriteDisabled("Binance USD-M quantity filter failed")
        if not _aligned(quantity, authority.filters.step_size):
            raise BrokerWriteDisabled("Binance USD-M quantity step filter failed")
        if command.order_style is OrderStyle.LIMIT:
            if command.time_in_force != "GTC":
                raise BrokerWriteDisabled(
                    "Binance USD-M limit time in force is invalid"
                )
            price = _decimal(command.limit_price, "limit_price")
            if price <= 0 or not _aligned(price, authority.filters.tick_size):
                raise BrokerWriteDisabled("Binance USD-M price tick filter failed")
        else:
            if command.time_in_force != "NONE" or command.limit_price is not None:
                raise BrokerWriteDisabled("Binance USD-M market order is invalid")
            price = _decimal(
                authority.notional_reference_price,
                "notional_reference_price",
            )
            if price <= 0:
                raise BrokerWriteDisabled("Binance USD-M notional reference is invalid")
        if (
            authority.role.opens_exposure
            and quantity * price < authority.filters.minimum_notional
        ):
            raise BrokerWriteDisabled("Binance USD-M minimum notional filter failed")
        if authority.role.opens_exposure:
            _validate_entry_authority(authority, now=now)
        else:
            _validate_close_authority(command, authority)
    except BrokerWriteDisabled:
        raise
    except TypeError, ValueError, InvalidOperation:
        raise BrokerWriteDisabled(
            "exact Binance USD-M write authority is invalid"
        ) from None


def _validate_recovery_command(command: BrokerOrderCommand) -> None:
    if type(command) is not BrokerOrderCommand:
        raise TypeError("exact BrokerOrderCommand is required")
    try:
        expected_client_id = binance_normal_client_order_id(command.id)
        _uuid7(command.account_id, "account_id")
        attempted_at = _utc(command.dispatch_attempted_at, "dispatch_attempted_at")
        not_after = _utc(command.not_after, "not_after")
    except TypeError, ValueError:
        raise BrokerWriteDisabled("Binance USD-M recovery command is invalid") from None
    if command.broker_client_order_id != expected_client_id:
        raise BrokerWriteDisabled(
            "Binance USD-M deterministic client ID does not match"
        )
    if (
        command.command_type is not CommandType.SUBMIT
        or command.target_broker_order_id is not None
        or command.replaces_command_id is not None
        or command.origin_type != "DAVID_V6_DECISION"
        or command.authority_class != "V6_PROVIDER_WRITE"
        or type(command.canonical_payload_hash) is not bytes
        or len(command.canonical_payload_hash) != 32
        or attempted_at >= not_after
    ):
        raise BrokerWriteDisabled("Binance USD-M recovery command is invalid")


def _validate_persisted_command(
    command: BrokerOrderCommand,
    record: BinanceUsdmNormalOrderRecord,
) -> None:
    record.validate()
    if (
        record.command_id != command.id
        or record.account_id != command.account_id
        or record.client_order_id != command.broker_client_order_id
        or record.request_digest != command.canonical_payload_hash
        or record.prepared_at != command.dispatch_attempted_at
        or record.not_after != command.not_after
    ):
        raise BrokerWriteDisabled(
            "Binance USD-M persisted command identity does not match"
        )


def _validate_entry_authority(
    authority: BinanceUsdmNormalOrderAuthority,
    *,
    now: datetime,
) -> None:
    if (
        type(authority.expected_leverage) is not int
        or not 1 <= authority.expected_leverage <= 7
        or type(authority.verified_leverage) is not int
        or authority.verified_leverage != authority.expected_leverage
        or not _fresh(authority.leverage_verified_at, now)
    ):
        raise BrokerWriteDisabled("Binance USD-M leverage authority is invalid")
    if authority.position_mode != "ONE_WAY":
        raise BrokerWriteDisabled("Binance USD-M ONE_WAY authority is required")
    if authority.margin_type != "ISOLATED":
        raise BrokerWriteDisabled("Binance USD-M ISOLATED authority is required")
    if authority.auto_add_margin is not False:
        raise BrokerWriteDisabled("Binance USD-M automatic margin addition is invalid")
    if authority.authorized_reduce_quantity != Decimal():
        raise BrokerWriteDisabled("Binance USD-M entry cannot carry reduce authority")


def _validate_close_authority(
    command: BrokerOrderCommand,
    authority: BinanceUsdmNormalOrderAuthority,
) -> None:
    if authority.position_mode != "ONE_WAY":
        raise BrokerWriteDisabled("Binance USD-M ONE_WAY authority is required")
    if command.order_style is not OrderStyle.MARKET:
        raise BrokerWriteDisabled("Binance USD-M close must be a market order")
    authorized = _decimal(
        authority.authorized_reduce_quantity,
        "authorized_reduce_quantity",
    )
    if authorized <= 0 or command.quantity != authorized:
        raise BrokerWriteDisabled("Binance USD-M close must cover full position")


def _decode_provider_order(
    response: BrokerResponse,
    record: BinanceUsdmNormalOrderRecord,
) -> _ProviderOrder:
    payload = _object(_json(response))
    expected = dict(parse_qsl(record.request_body.decode("ascii"), strict_parsing=True))
    order_id = _integer(payload.get("orderId"), "orderId", positive=True)
    client_order_id = _text(payload.get("clientOrderId"), "clientOrderId")
    symbol = _text(payload.get("symbol"), "symbol")
    status = _text(payload.get("status"), "status")
    side = Side(_text(payload.get("side"), "side"))
    order_type = OrderStyle(_text(payload.get("type"), "type"))
    original_quantity = _provider_decimal(payload.get("origQty"), "origQty")
    executed_quantity = _provider_decimal(payload.get("executedQty"), "executedQty")
    cumulative_quote = _provider_decimal(payload.get("cumQuote"), "cumQuote")
    average_price = _provider_decimal(payload.get("avgPrice"), "avgPrice")
    reduce_only = _boolean(payload.get("reduceOnly"), "reduceOnly")
    position_side = _text(payload.get("positionSide"), "positionSide")
    expected_reduce_only = expected.get("reduceOnly") == "true"
    if (
        client_order_id != record.client_order_id
        or symbol != expected.get("symbol")
        or side.value != expected.get("side")
        or order_type.value != expected.get("type")
        or original_quantity
        != _provider_decimal(expected.get("quantity"), "expected quantity")
        or reduce_only is not expected_reduce_only
        or position_side != "BOTH"
        or status not in _ALLOWED_ORDER_STATUSES
        or executed_quantity < 0
        or executed_quantity > original_quantity
        or cumulative_quote < 0
        or average_price < 0
    ):
        raise ValueError("Binance USD-M normal order response is invalid")
    return _ProviderOrder(
        order_id=order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        status=status,
        side=side,
        order_type=order_type,
        original_quantity=original_quantity,
        executed_quantity=executed_quantity,
        cumulative_quote=cumulative_quote,
        average_price=average_price,
        reduce_only=reduce_only,
        position_side=position_side,
    )


def _decode_fills(
    response: BrokerResponse,
    order: _ProviderOrder,
) -> tuple[BinanceUsdmFill, ...]:
    payload = _json(response)
    if not isinstance(payload, list):
        raise ValueError("Binance USD-M fill response is invalid")
    rows = cast(list[object], payload)
    fills: list[BinanceUsdmFill] = []
    for raw in rows:
        row = _object(raw)
        fill = BinanceUsdmFill(
            trade_id=_integer(row.get("id"), "trade id", positive=False),
            order_id=_integer(row.get("orderId"), "orderId", positive=True),
            side=Side(_text(row.get("side"), "side")),
            quantity=_provider_decimal(row.get("qty"), "qty"),
            price=_provider_decimal(row.get("price"), "price"),
            commission=_provider_decimal(row.get("commission"), "commission"),
            commission_asset=_text(row.get("commissionAsset"), "commissionAsset"),
            realized_pnl=_provider_decimal(row.get("realizedPnl"), "realizedPnl"),
            occurred_at=_provider_time(row.get("time")),
        )
        if (
            row.get("symbol") != _SYMBOL
            or fill.order_id != order.order_id
            or fill.side is not order.side
        ):
            raise ValueError("Binance USD-M fill identity is invalid")
        fills.append(fill)
    result = tuple(sorted(fills, key=lambda fill: fill.trade_id))
    if len(result) != len({fill.trade_id for fill in result}):
        raise ValueError("duplicate Binance USD-M trade ID")
    return result


def _match_cumulative_order_facts(
    order: _ProviderOrder,
    fills: tuple[BinanceUsdmFill, ...],
) -> None:
    filled_quantity = sum((fill.quantity for fill in fills), start=Decimal())
    quote_quantity = sum(
        (fill.quantity * fill.price for fill in fills),
        start=Decimal(),
    )
    if (
        filled_quantity != order.executed_quantity
        or quote_quantity != order.cumulative_quote
    ):
        raise ValueError("Binance USD-M cumulative fill evidence does not match")
    expected_average = (
        Decimal() if filled_quantity == 0 else quote_quantity / filled_quantity
    )
    if order.average_price != expected_average:
        raise ValueError("Binance USD-M average fill price does not match")


def _is_order_not_found(response: BrokerResponse) -> bool:
    if response.status not in {400, 404}:
        return False
    try:
        payload = _object(_json(response))
    except ValueError:
        return False
    return payload.get("code") == -2013


def _post_response_is_authoritative_rejection(response: BrokerResponse) -> bool:
    if not 400 <= response.status < 500:
        return False
    try:
        payload = _object(_json(response))
    except ValueError:
        return False
    code = payload.get("code")
    if type(code) is not int or code in _AMBIGUOUS_POST_ERROR_CODES:
        return False
    return code in _AUTHORITATIVE_POST_ERROR_CODES or any(
        lower <= code <= upper for lower, upper in _AUTHORITATIVE_POST_ERROR_RANGES
    )


def _json(response: BrokerResponse) -> object:
    try:
        return cast(object, json.loads(response.body))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Binance USD-M response is invalid") from error


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Binance USD-M response object is invalid")
    raw = cast(dict[object, object], value)
    if any(type(key) is not str for key in raw):
        raise ValueError("Binance USD-M response object is invalid")
    return cast(dict[str, object], raw)


def _text(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or not value.isascii()
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError(f"Binance USD-M {name} is invalid")
    return value


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"Binance USD-M {name} is invalid")
    return value


def _integer(value: object, name: str, *, positive: bool) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or value < minimum:
        raise ValueError(f"Binance USD-M {name} is invalid")
    return value


def _provider_decimal(value: object, name: str) -> Decimal:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"Binance USD-M {name} is invalid")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"Binance USD-M {name} is invalid") from error
    if not result.is_finite():
        raise ValueError(f"Binance USD-M {name} is invalid")
    return result


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"Binance USD-M {name} must be an exact Decimal")
    return value


def _decimal_text(value: object) -> str:
    exact = _decimal(value, "request decimal")
    result = format(exact, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if result in {"", "-0"} else result


def _aligned(value: Decimal, quantum: Decimal) -> bool:
    return value % quantum == 0


def _asset(value: object) -> str:
    text = _text(value, "asset")
    if not text.isalnum() or text != text.upper():
        raise ValueError("Binance USD-M asset is invalid")
    return text


def _client_order_id(value: object) -> str:
    text = _text(value, "client order ID")
    if not 1 <= len(text) <= 36 or any(
        character
        not in (".:/_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
        for character in text
    ):
        raise ValueError("Binance USD-M client order ID is invalid")
    return text


def _provider_order_id(value: object) -> str:
    text = _text(value, "provider order ID")
    prefix = "BINANCE-USDM:"
    suffix = text.removeprefix(prefix)
    if not text.startswith(prefix) or not suffix.isascii() or not suffix.isdecimal():
        raise ValueError("Binance USD-M provider order ID is invalid")
    return text


def _uuid7(value: object, name: str) -> UUID:
    if type(value) is not UUID or value.version != 7:
        raise ValueError(f"{name} must be UUIDv7")
    return value


def _utc(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is not UTC
        or value.utcoffset() != timedelta(0)
        or value.microsecond != 0
    ):
        raise ValueError(f"Binance USD-M {name} must use whole-second UTC")
    return value


def _utc_instant(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is not UTC
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"Binance USD-M {name} must use exact UTC")
    return value


def _provider_time(value: object) -> datetime:
    if type(value) is not int or value < 0:
        raise ValueError("Binance USD-M provider time is invalid")
    seconds, milliseconds = divmod(value, 1000)
    try:
        return datetime.fromtimestamp(seconds, tz=UTC).replace(
            microsecond=milliseconds * 1000
        )
    except (OSError, OverflowError, ValueError) as error:
        raise ValueError("Binance USD-M provider time is invalid") from error


def _fresh(captured_at: datetime, now: datetime) -> bool:
    captured = _utc(captured_at, "authority captured_at")
    age = now - captured
    return timedelta() <= age <= _AUTHORITY_MAX_AGE


__all__ = (
    "BinanceUsdmFill",
    "BinanceUsdmNormalOrderAuthority",
    "BinanceUsdmNormalOrderClaim",
    "BinanceUsdmNormalOrderRecord",
    "BinanceUsdmNormalOrderState",
    "BinanceUsdmOrderNotSent",
    "BinanceUsdmOrderRejected",
    "BinanceUsdmOrderRole",
    "BinanceUsdmOrderService",
    "BinanceUsdmOrderUnknown",
    "BinanceUsdmPreSendFailure",
    "BinanceUsdmSymbolFilters",
    "BrokerWriteResult",
    "binance_normal_client_order_id",
    "build_binance_usdm_order_request",
)
