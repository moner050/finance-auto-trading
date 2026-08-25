from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
from typing import Protocol, cast
from urllib.parse import parse_qsl, urlencode
from uuid import UUID

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.binance_usdm.orders import (
    BinanceUsdmOrderUnknown,
    BrokerWriteResult,
    binance_normal_client_order_id,
)
from autotrader.integrations.brokers.common import (
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)
from autotrader.risk.v6 import V6RiskAuthority

_SYMBOL = "BTCUSDT"
_ACTIVE_ALGO_STATUS = "NEW"
_AMBIGUOUS_POST_ERROR_CODES = frozenset({-1000, -1006, -1007, -4116})
_AUTHORITATIVE_POST_ERROR_CODES = frozenset({-1008})
_AUTHORITATIVE_POST_ERROR_RANGES = (
    (-1999, -1100),
    (-2999, -2000),
    (-4999, -4000),
)


class BinanceUsdmProtectionUnknown(RuntimeError):
    """Protection is not proven and the algo order must never be re-posted."""


class BinanceUsdmProtectionRejected(RuntimeError):
    """Binance authoritatively rejected protection for an exposed position."""


class BinanceUsdmProtectionEmergencyUnknown(RuntimeError):
    """The emergency close or account safety transition is not proven."""


class BinanceUsdmAlgoOrderState(StrEnum):
    PREPARED = "PREPARED"
    AMBIGUOUS = "AMBIGUOUS"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    EMERGENCY_CLOSED = "EMERGENCY_CLOSED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class EntryFill:
    entry_command_id: UUID
    account_id: UUID
    instrument_id: UUID
    binding_id: UUID
    side: Side
    first_fill_quantity: Decimal
    cumulative_quantity_before: Decimal
    average_fill_price: Decimal
    symbol: str
    tick_size: Decimal
    filled_at: datetime
    protection_deadline: datetime
    emergency_close_command_id: UUID

    def __post_init__(self) -> None:
        for name in (
            "entry_command_id",
            "account_id",
            "instrument_id",
            "binding_id",
        ):
            _uuid7(getattr(self, name), name)
        if type(self.side) is not Side:
            raise TypeError("Binance USD-M entry fill side must be exact")
        if _decimal(self.first_fill_quantity, "first fill quantity") <= 0:
            raise ValueError("Binance USD-M first fill quantity must be positive")
        if _decimal(self.cumulative_quantity_before, "prior fill quantity") < 0:
            raise ValueError("Binance USD-M prior fill quantity cannot be negative")
        if _decimal(self.average_fill_price, "average fill price") <= 0:
            raise ValueError("Binance USD-M average fill price must be positive")
        if self.symbol != _SYMBOL:
            raise ValueError("Binance USD-M protection supports BTCUSDT only")
        if _decimal(self.tick_size, "tick size") <= 0:
            raise ValueError("Binance USD-M tick size must be positive")
        filled_at = _utc(self.filled_at, "fill time")
        deadline = _utc(self.protection_deadline, "protection deadline")
        if filled_at >= deadline:
            raise ValueError("Binance USD-M protection deadline is invalid")
        _uuid7(self.emergency_close_command_id, "emergency_close_command_id")


@dataclass(frozen=True, slots=True)
class ProtectionResult:
    provider_algo_id: str | None
    client_algo_id: str
    state: BinanceUsdmAlgoOrderState
    trigger_price: Decimal
    recovered: bool
    emergency_close: BrokerWriteResult | None

    def __post_init__(self) -> None:
        _client_algo_id(self.client_algo_id)
        if type(self.state) is not BinanceUsdmAlgoOrderState:
            raise TypeError("Binance USD-M protection result state must be exact")
        if _decimal(self.trigger_price, "trigger price") <= 0:
            raise ValueError("Binance USD-M trigger price must be positive")
        if type(self.recovered) is not bool:
            raise TypeError("Binance USD-M recovered must be bool")
        if self.state is BinanceUsdmAlgoOrderState.ACTIVE:
            _provider_algo_id(self.provider_algo_id)
            if self.emergency_close is not None:
                raise ValueError("active protection cannot have an emergency close")
        elif self.state is BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED:
            if self.provider_algo_id is not None:
                _provider_algo_id(self.provider_algo_id)
            if type(self.emergency_close) is not BrokerWriteResult:
                raise ValueError("emergency protection needs an exact close result")
        else:
            raise ValueError("Binance USD-M protection result is not terminal safe")


@dataclass(frozen=True, slots=True)
class BinanceUsdmAlgoOrderRecord:
    entry_fill: EntryFill
    client_algo_id: str
    trigger_price: Decimal
    request_body: bytes
    request_digest: bytes
    prepared_at: datetime
    state: BinanceUsdmAlgoOrderState
    result: ProtectionResult | None

    @classmethod
    def prepared(
        cls,
        *,
        fill: EntryFill,
        trigger_price: Decimal,
        request: BrokerRequest,
        prepared_at: datetime,
    ) -> BinanceUsdmAlgoOrderRecord:
        if request.method != "POST" or request.path != "/fapi/v1/algoOrder":
            raise ValueError("Binance USD-M canonical algo request is invalid")
        body = request.body
        if type(body) is not bytes or not body:
            raise ValueError("Binance USD-M canonical algo body is invalid")
        result = cls(
            entry_fill=fill,
            client_algo_id=binance_protection_client_algo_id(fill.entry_command_id),
            trigger_price=trigger_price,
            request_body=body,
            request_digest=sha256(body).digest(),
            prepared_at=prepared_at,
            state=BinanceUsdmAlgoOrderState.PREPARED,
            result=None,
        )
        result.validate()
        return result

    def validate(self) -> None:
        if type(self.entry_fill) is not EntryFill:
            raise TypeError("Binance USD-M entry fill must be exact")
        self.entry_fill.__post_init__()
        if self.client_algo_id != binance_protection_client_algo_id(
            self.entry_fill.entry_command_id
        ):
            raise ValueError("Binance USD-M persisted client algo ID is invalid")
        if _decimal(self.trigger_price, "trigger price") <= 0:
            raise ValueError("Binance USD-M persisted trigger price is invalid")
        if type(self.request_body) is not bytes or not self.request_body:
            raise ValueError("Binance USD-M persisted algo body is invalid")
        if (
            type(self.request_digest) is not bytes
            or len(self.request_digest) != 32
            or sha256(self.request_body).digest() != self.request_digest
        ):
            raise ValueError("Binance USD-M persisted algo digest is invalid")
        prepared_at = _utc(self.prepared_at, "algo prepared_at")
        if prepared_at < self.entry_fill.filled_at:
            raise ValueError("Binance USD-M algo preparation predates the fill")
        if type(self.state) is not BinanceUsdmAlgoOrderState:
            raise TypeError("Binance USD-M algo state must be exact")
        if self.state in {
            BinanceUsdmAlgoOrderState.ACTIVE,
            BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED,
        }:
            if type(self.result) is not ProtectionResult:
                raise ValueError("safe Binance USD-M protection needs a result")
            if self.result.state is not self.state:
                raise ValueError("Binance USD-M protection result state differs")
            if self.result.client_algo_id != self.client_algo_id:
                raise ValueError("Binance USD-M protection result identity differs")
        elif self.result is not None:
            raise ValueError("unsafe Binance USD-M protection cannot have a result")


@dataclass(frozen=True, slots=True)
class BinanceUsdmAlgoOrderClaim:
    record: BinanceUsdmAlgoOrderRecord
    acquired: bool

    def __post_init__(self) -> None:
        if type(self.record) is not BinanceUsdmAlgoOrderRecord:
            raise TypeError("Binance USD-M algo record must be exact")
        self.record.validate()
        if type(self.acquired) is not bool:
            raise TypeError("Binance USD-M algo claim acquired must be bool")
        if (
            self.acquired
            and self.record.state is not BinanceUsdmAlgoOrderState.PREPARED
        ):
            raise ValueError("only prepared Binance USD-M protection may be acquired")


class BinanceUsdmAlgoOrderStore(Protocol):
    async def prepare(
        self, record: BinanceUsdmAlgoOrderRecord
    ) -> BinanceUsdmAlgoOrderClaim: ...

    async def load_by_client_algo_id(
        self, client_algo_id: str
    ) -> BinanceUsdmAlgoOrderRecord | None: ...

    async def finish(
        self,
        client_algo_id: str,
        *,
        state: BinanceUsdmAlgoOrderState,
        result: ProtectionResult | None,
    ) -> BinanceUsdmAlgoOrderRecord: ...


class BinanceUsdmAlgoOrderSender(Protocol):
    async def send(self, request: BrokerRequest) -> BrokerResponse: ...


class BinanceUsdmEmergencyOrderService(Protocol):
    async def prepare_full_close(self, fill: EntryFill) -> BrokerOrderCommand: ...

    async def submit_locked(self, command: BrokerOrderCommand) -> BrokerWriteResult: ...

    async def recover_by_client_id(self, client_order_id: str) -> BrokerWriteResult: ...

    async def confirm_zero_position(self, fill: EntryFill) -> bool: ...


class BinanceUsdmProtectionSafetyActions(Protocol):
    async def cancel_entry_and_adds(self, binding_id: UUID) -> None: ...

    async def halt_account(self, binding_id: UUID, reason: str) -> None: ...


class BinanceUsdmProtectionService:
    def __init__(
        self,
        *,
        store: BinanceUsdmAlgoOrderStore,
        sender: BinanceUsdmAlgoOrderSender,
        emergency_orders: BinanceUsdmEmergencyOrderService,
        safety_actions: BinanceUsdmProtectionSafetyActions,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(clock):
            raise TypeError("Binance USD-M protection clock must be callable")
        self._store = store
        self._sender = sender
        self._emergency_orders = emergency_orders
        self._safety_actions = safety_actions
        self._clock = clock

    async def protect_first_fill(
        self,
        fill: EntryFill,
        authority: V6RiskAuthority,
    ) -> ProtectionResult:
        if type(fill) is not EntryFill:
            raise TypeError("exact Binance USD-M EntryFill is required")
        fill.__post_init__()
        client_algo_id = binance_protection_client_algo_id(fill.entry_command_id)
        existing = await self._store.load_by_client_algo_id(client_algo_id)
        if existing is not None:
            if type(existing) is not BinanceUsdmAlgoOrderRecord:
                raise BinanceUsdmProtectionUnknown(
                    "Binance USD-M durable protection record is invalid"
                )
            _validate_persisted_fill(fill, existing)
            return await self._existing_or_recover(existing)
        _validate_initial_fill(fill)
        trigger_price = _validate_risk_authority(fill, authority)
        request = build_binance_usdm_protection_request(fill, trigger_price)
        now = _utc(self._clock(), "protection clock")
        if now < fill.filled_at:
            raise BrokerWriteDisabled("Binance USD-M protection clock predates fill")
        prepared = BinanceUsdmAlgoOrderRecord.prepared(
            fill=fill,
            trigger_price=trigger_price,
            request=request,
            prepared_at=now,
        )
        claim = await self._store.prepare(prepared)
        if type(claim) is not BinanceUsdmAlgoOrderClaim:
            raise BinanceUsdmProtectionUnknown(
                "Binance USD-M durable protection claim is invalid"
            )
        record = claim.record
        if not claim.acquired:
            _validate_persisted_fill(fill, record)
            return await self._existing_or_recover(record)
        if now >= fill.protection_deadline:
            await self._fail_safe(record, reason="BINANCE_PROTECTION_DEADLINE")
            raise BinanceUsdmProtectionUnknown(
                "Binance USD-M protection deadline required emergency close"
            )
        try:
            response = await self._sender.send(request)
        except asyncio.CancelledError, KeyboardInterrupt, SystemExit:
            await self._mark_ambiguous(client_algo_id)
            raise
        except Exception:
            await self._mark_ambiguous(client_algo_id)
            if self._deadline_reached(record):
                await self._fail_safe(
                    record,
                    reason="BINANCE_PROTECTION_DEADLINE",
                )
                raise BinanceUsdmProtectionUnknown(
                    "Binance USD-M protection deadline required emergency close"
                ) from None
            raise BinanceUsdmProtectionUnknown(
                "Binance USD-M protection dispatch outcome is unknown"
            ) from None
        return await self._accept_post_response(record, response)

    async def recover_by_client_algo_id(self, client_algo_id: str) -> ProtectionResult:
        _client_algo_id(client_algo_id)
        record = await self._store.load_by_client_algo_id(client_algo_id)
        if type(record) is not BinanceUsdmAlgoOrderRecord:
            raise BinanceUsdmProtectionUnknown(
                "Binance USD-M durable protection record is unavailable"
            )
        record.validate()
        if record.state in {
            BinanceUsdmAlgoOrderState.ACTIVE,
            BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED,
        }:
            assert type(record.result) is ProtectionResult
            return record.result
        if record.state is BinanceUsdmAlgoOrderState.UNKNOWN:
            return await self._fail_safe(
                record,
                reason="BINANCE_PROTECTION_RESTART",
            )
        try:
            response = await self._sender.send(
                BrokerRequest(
                    method="GET",
                    path=(
                        "/fapi/v1/algoOrder?"
                        + urlencode((("clientAlgoId", client_algo_id),))
                    ),
                )
            )
        except asyncio.CancelledError, KeyboardInterrupt, SystemExit:
            raise
        except Exception:
            if self._deadline_reached(record):
                await self._fail_safe(
                    record,
                    reason="BINANCE_PROTECTION_DEADLINE",
                )
                raise BinanceUsdmProtectionUnknown(
                    "Binance USD-M protection deadline required emergency close"
                ) from None
            raise BinanceUsdmProtectionUnknown(
                "Binance USD-M protection recovery is unavailable"
            ) from None
        if _is_algo_not_found(response):
            if not self._deadline_reached(record):
                raise BinanceUsdmProtectionUnknown(
                    "Binance USD-M protection is not yet found"
                )
            await self._fail_safe(record, reason="BINANCE_PROTECTION_DEADLINE")
            raise BinanceUsdmProtectionUnknown(
                "Binance USD-M protection deadline required emergency close"
            )
        if response.status != 200:
            if self._deadline_reached(record):
                await self._fail_safe(
                    record,
                    reason="BINANCE_PROTECTION_DEADLINE",
                )
                raise BinanceUsdmProtectionUnknown(
                    "Binance USD-M protection deadline required emergency close"
                ) from None
            raise BinanceUsdmProtectionUnknown(
                "Binance USD-M protection recovery is unavailable"
            )
        try:
            result = _decode_active_protection(response, record, recovered=True)
        except TypeError, ValueError:
            if self._deadline_reached(record):
                await self._fail_safe(
                    record,
                    reason="BINANCE_PROTECTION_DEADLINE",
                )
                raise BinanceUsdmProtectionUnknown(
                    "Binance USD-M protection deadline required emergency close"
                ) from None
            raise BinanceUsdmProtectionUnknown(
                "Binance USD-M recovered protection evidence is invalid"
            ) from None
        if self._deadline_reached(record):
            await self._fail_safe(
                record,
                reason="BINANCE_PROTECTION_DEADLINE",
                observed_algo_id=result.provider_algo_id,
            )
            raise BinanceUsdmProtectionUnknown(
                "Binance USD-M late protection required emergency close"
            )
        await self._store.finish(
            client_algo_id,
            state=BinanceUsdmAlgoOrderState.ACTIVE,
            result=result,
        )
        return result

    async def _existing_or_recover(
        self, record: BinanceUsdmAlgoOrderRecord
    ) -> ProtectionResult:
        if record.state in {
            BinanceUsdmAlgoOrderState.ACTIVE,
            BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED,
        }:
            assert type(record.result) is ProtectionResult
            return record.result
        return await self.recover_by_client_algo_id(record.client_algo_id)

    async def _accept_post_response(
        self,
        record: BinanceUsdmAlgoOrderRecord,
        response: BrokerResponse,
    ) -> ProtectionResult:
        if (
            response.status == 408
            or response.status >= 500
            or (
                400 <= response.status < 500
                and not _post_response_is_authoritative_rejection(response)
            )
        ):
            await self._mark_ambiguous(record.client_algo_id)
            if self._deadline_reached(record):
                await self._fail_safe(
                    record,
                    reason="BINANCE_PROTECTION_DEADLINE",
                )
                raise BinanceUsdmProtectionUnknown(
                    "Binance USD-M protection deadline required emergency close"
                )
            raise BinanceUsdmProtectionUnknown(
                "Binance USD-M protection dispatch outcome is unknown"
            )
        if 400 <= response.status < 500:
            await self._store.finish(
                record.client_algo_id,
                state=BinanceUsdmAlgoOrderState.REJECTED,
                result=None,
            )
            await self._fail_safe(
                record,
                reason="BINANCE_PROTECTION_REJECTED",
            )
            raise BinanceUsdmProtectionRejected(
                "Binance USD-M protection rejection required emergency close"
            )
        if response.status != 200:
            await self._mark_ambiguous(record.client_algo_id)
            raise BinanceUsdmProtectionUnknown(
                "Binance USD-M protection dispatch outcome is unknown"
            )
        try:
            result = _decode_active_protection(response, record, recovered=False)
        except TypeError, ValueError:
            await self._mark_ambiguous(record.client_algo_id)
            if self._deadline_reached(record):
                await self._fail_safe(
                    record,
                    reason="BINANCE_PROTECTION_DEADLINE",
                )
                raise BinanceUsdmProtectionUnknown(
                    "Binance USD-M protection deadline required emergency close"
                ) from None
            raise BinanceUsdmProtectionUnknown(
                "Binance USD-M protection acknowledgement is unknown"
            ) from None
        if self._deadline_reached(record):
            await self._fail_safe(
                record,
                reason="BINANCE_PROTECTION_DEADLINE",
                observed_algo_id=result.provider_algo_id,
            )
            raise BinanceUsdmProtectionUnknown(
                "Binance USD-M late protection required emergency close"
            )
        await self._store.finish(
            record.client_algo_id,
            state=BinanceUsdmAlgoOrderState.ACTIVE,
            result=result,
        )
        return result

    async def _fail_safe(
        self,
        record: BinanceUsdmAlgoOrderRecord,
        *,
        reason: str,
        observed_algo_id: str | None = None,
    ) -> ProtectionResult:
        if record.state is BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED:
            assert type(record.result) is ProtectionResult
            return record.result
        fill = record.entry_fill
        command: BrokerOrderCommand | None = None
        emergency_result: BrokerWriteResult | None = None
        failed = False
        try:
            command = await self._emergency_orders.prepare_full_close(fill)
            _validate_emergency_command(fill, command)
            try:
                emergency_result = await self._emergency_orders.submit_locked(command)
            except BinanceUsdmOrderUnknown:
                emergency_result = await self._emergency_orders.recover_by_client_id(
                    command.broker_client_order_id
                )
            _validate_emergency_result(command, emergency_result)
        except asyncio.CancelledError, KeyboardInterrupt, SystemExit:
            failed = True
            raise
        except Exception:
            failed = True
        try:
            await self._safety_actions.cancel_entry_and_adds(fill.binding_id)
        except asyncio.CancelledError, KeyboardInterrupt, SystemExit:
            failed = True
            raise
        except Exception:
            failed = True
        try:
            zero_position = await self._emergency_orders.confirm_zero_position(fill)
            if zero_position is not True:
                failed = True
        except asyncio.CancelledError, KeyboardInterrupt, SystemExit:
            failed = True
            raise
        except Exception:
            failed = True
        try:
            await self._safety_actions.halt_account(fill.binding_id, reason)
        except asyncio.CancelledError, KeyboardInterrupt, SystemExit:
            failed = True
            raise
        except Exception:
            failed = True
        if failed or emergency_result is None:
            await self._store.finish(
                record.client_algo_id,
                state=BinanceUsdmAlgoOrderState.UNKNOWN,
                result=None,
            )
            raise BinanceUsdmProtectionEmergencyUnknown(
                "Binance USD-M emergency protection outcome is unknown"
            )
        result = ProtectionResult(
            provider_algo_id=observed_algo_id,
            client_algo_id=record.client_algo_id,
            state=BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED,
            trigger_price=record.trigger_price,
            recovered=False,
            emergency_close=emergency_result,
        )
        await self._store.finish(
            record.client_algo_id,
            state=BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED,
            result=result,
        )
        return result

    def _deadline_reached(self, record: BinanceUsdmAlgoOrderRecord) -> bool:
        now = _utc(self._clock(), "protection clock")
        return now >= record.entry_fill.protection_deadline

    async def _mark_ambiguous(self, client_algo_id: str) -> None:
        await self._store.finish(
            client_algo_id,
            state=BinanceUsdmAlgoOrderState.AMBIGUOUS,
            result=None,
        )


def binance_protection_client_algo_id(entry_command_id: UUID) -> str:
    _uuid7(entry_command_id, "entry_command_id")
    return f"v6s-{entry_command_id.hex}"


def build_binance_usdm_protection_request(
    fill: EntryFill,
    trigger_price: Decimal,
) -> BrokerRequest:
    if type(fill) is not EntryFill:
        raise TypeError("exact Binance USD-M EntryFill is required")
    fill.__post_init__()
    trigger = _decimal(trigger_price, "trigger price")
    if trigger <= 0 or trigger % fill.tick_size != 0:
        raise ValueError("Binance USD-M trigger price is not tick aligned")
    exit_side = Side.SELL if fill.side is Side.BUY else Side.BUY
    parameters = (
        ("algoType", "CONDITIONAL"),
        ("symbol", _SYMBOL),
        ("side", exit_side.value),
        ("positionSide", "BOTH"),
        ("type", "STOP_MARKET"),
        ("triggerPrice", _decimal_text(trigger)),
        ("workingType", "MARK_PRICE"),
        ("closePosition", "true"),
        ("priceProtect", "false"),
        ("clientAlgoId", binance_protection_client_algo_id(fill.entry_command_id)),
        ("newOrderRespType", "RESULT"),
    )
    return BrokerRequest(
        method="POST",
        path="/fapi/v1/algoOrder",
        headers=(("Content-Type", "application/x-www-form-urlencoded"),),
        body=urlencode(parameters).encode("ascii"),
    )


def _validate_initial_fill(fill: EntryFill) -> None:
    if fill.cumulative_quantity_before != Decimal():
        raise BrokerWriteDisabled(
            "Binance USD-M protection requires the first non-zero fill"
        )


def _validate_risk_authority(
    fill: EntryFill,
    authority: V6RiskAuthority,
) -> Decimal:
    if type(authority) is not V6RiskAuthority:
        raise BrokerWriteDisabled("exact Binance USD-M v6 risk authority is absent")
    try:
        stop = _decimal(authority.stop_price, "authority stop price")
        quantity = _decimal(authority.quantity, "authority quantity")
        if (
            authority.allowed is not True
            or authority.blocker_codes != ()
            or quantity < fill.first_fill_quantity
            or stop <= 0
            or (fill.side is Side.BUY and stop >= fill.average_fill_price)
            or (fill.side is Side.SELL and stop <= fill.average_fill_price)
        ):
            raise ValueError
        rounding = ROUND_FLOOR if fill.side is Side.BUY else ROUND_CEILING
        trigger = (stop / fill.tick_size).to_integral_value(
            rounding=rounding
        ) * fill.tick_size
        if trigger <= 0:
            raise ValueError
        return trigger
    except InvalidOperation, TypeError, ValueError:
        raise BrokerWriteDisabled(
            "exact Binance USD-M v6 risk authority is invalid"
        ) from None


def _validate_emergency_command(
    fill: EntryFill,
    command: BrokerOrderCommand,
) -> None:
    if type(command) is not BrokerOrderCommand:
        raise TypeError("Binance USD-M emergency command must be exact")
    expected_side = Side.SELL if fill.side is Side.BUY else Side.BUY
    if (
        command.id != fill.emergency_close_command_id
        or command.account_id != fill.account_id
        or command.instrument_id != fill.instrument_id
        or command.command_type is not CommandType.SUBMIT
        or command.side is not expected_side
        or command.order_style is not OrderStyle.MARKET
        or command.limit_price is not None
        or command.time_in_force != "NONE"
        or command.target_broker_order_id is not None
        or command.replaces_command_id is not None
        or command.origin_type != "DAVID_V6_DECISION"
        or command.authority_class != "V6_PROVIDER_WRITE"
        or command.broker_client_order_id != binance_normal_client_order_id(command.id)
        or _decimal(command.quantity, "emergency quantity") <= 0
    ):
        raise ValueError("Binance USD-M emergency close command is invalid")


def _validate_emergency_result(
    command: BrokerOrderCommand,
    result: object,
) -> None:
    if type(result) is not BrokerWriteResult:
        raise ValueError("Binance USD-M emergency close result is invalid")
    result.__post_init__()
    if (
        result.client_order_id != command.broker_client_order_id
        or result.provider_state != "FILLED"
        or result.cumulative_filled_quantity != command.quantity
    ):
        raise ValueError("Binance USD-M emergency close is not fully reconciled")


def _validate_persisted_fill(
    fill: EntryFill,
    record: BinanceUsdmAlgoOrderRecord,
) -> None:
    record.validate()
    if record.entry_fill != fill:
        raise BrokerWriteDisabled(
            "Binance USD-M persisted protection identity does not match"
        )


def _decode_active_protection(
    response: BrokerResponse,
    record: BinanceUsdmAlgoOrderRecord,
    *,
    recovered: bool,
) -> ProtectionResult:
    payload = _object(_json(response))
    expected = dict(parse_qsl(record.request_body.decode("ascii"), strict_parsing=True))
    algo_id = _integer(payload.get("algoId"), "algoId")
    client_algo_id = _text(payload.get("clientAlgoId"), "clientAlgoId")
    trigger_price = _provider_decimal(payload.get("triggerPrice"), "triggerPrice")
    if (
        client_algo_id != record.client_algo_id
        or payload.get("algoType") != "CONDITIONAL"
        or payload.get("orderType") != "STOP_MARKET"
        or payload.get("symbol") != _SYMBOL
        or payload.get("side") != expected.get("side")
        or payload.get("positionSide") != "BOTH"
        or payload.get("algoStatus") != _ACTIVE_ALGO_STATUS
        or trigger_price != record.trigger_price
        or payload.get("workingType") != "MARK_PRICE"
        or payload.get("closePosition") is not True
        or payload.get("priceProtect") is not False
        or payload.get("reduceOnly") not in {None, False}
    ):
        raise ValueError("Binance USD-M active protection response is invalid")
    return ProtectionResult(
        provider_algo_id=f"BINANCE-USDM-ALGO:{algo_id}",
        client_algo_id=client_algo_id,
        state=BinanceUsdmAlgoOrderState.ACTIVE,
        trigger_price=trigger_price,
        recovered=recovered,
        emergency_close=None,
    )


def _is_algo_not_found(response: BrokerResponse) -> bool:
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


def _integer(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"Binance USD-M {name} is invalid")
    return value


def _provider_decimal(value: object, name: str) -> Decimal:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"Binance USD-M {name} is invalid")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"Binance USD-M {name} is invalid") from error
    if not result.is_finite() or result < 0:
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


def _client_algo_id(value: object) -> str:
    text = _text(value, "client algo ID")
    if not 1 <= len(text) <= 36 or any(
        character
        not in (".:/_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
        for character in text
    ):
        raise ValueError("Binance USD-M client algo ID is invalid")
    return text


def _provider_algo_id(value: object) -> str:
    text = _text(value, "provider algo ID")
    prefix = "BINANCE-USDM-ALGO:"
    suffix = text.removeprefix(prefix)
    if not text.startswith(prefix) or not suffix.isascii() or not suffix.isdecimal():
        raise ValueError("Binance USD-M provider algo ID is invalid")
    return text


__all__ = (
    "BinanceUsdmAlgoOrderClaim",
    "BinanceUsdmAlgoOrderRecord",
    "BinanceUsdmAlgoOrderState",
    "BinanceUsdmProtectionEmergencyUnknown",
    "BinanceUsdmProtectionRejected",
    "BinanceUsdmProtectionService",
    "BinanceUsdmProtectionUnknown",
    "EntryFill",
    "ProtectionResult",
    "binance_protection_client_algo_id",
    "build_binance_usdm_protection_request",
)
