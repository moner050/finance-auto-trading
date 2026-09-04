from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.common import (
    BrokerSubmissionRejected,
    BrokerWriteDisabled,
    writes_to_a_venue,
)
from autotrader.integrations.brokers.kis.cash_order_contracts import (
    KisCashAccount,
    LockedOrderIntent,
    ProviderOrderIdentity,
    build_cash_cancel_request,
    build_cash_order_request,
)
from autotrader.integrations.brokers.kis.cash_order_recovery import (
    KisAmbiguousDispatch,
    KisDailyOrder,
    KisRecoveryStatus,
    recover_ambiguous_cash_order,
)
from autotrader.integrations.brokers.kis.cash_order_transport import (
    KisCashOrderTransport,
    KisDispatchResult,
    KisDispatchState,
)

_KST = ZoneInfo("Asia/Seoul")
_STRATEGY_VERSION = "david-trullas-v6.0"


class KisCashWriterUnknown(RuntimeError):
    """KIS may have accepted the write; blind submission is forbidden."""


class KisCashWriterRejected(BrokerSubmissionRejected):
    """KIS authoritatively rejected the write."""


@dataclass(frozen=True, slots=True)
class BrokerWriteResult:
    broker_order_id: str
    provider_state: str

    def __post_init__(self) -> None:
        if not self.broker_order_id.startswith("KIS-KRX:"):
            raise ValueError("canonical KIS broker order identity is required")
        if self.provider_state not in {
            KisDispatchState.ACKNOWLEDGED.value,
            "RECOVERED",
        }:
            raise ValueError("KIS provider state is invalid")


@dataclass(frozen=True, slots=True)
class KisCashWriteContext:
    command_id: UUID
    instrument_id: UUID
    intent: LockedOrderIntent
    account: KisCashAccount
    provider_binding_id: UUID
    binding_generation: int
    policy_version_id: UUID
    strategy_version: str
    writer_capability: bool
    target_order: ProviderOrderIdentity | None

    def __post_init__(self) -> None:
        for name in (
            "command_id",
            "instrument_id",
            "provider_binding_id",
            "policy_version_id",
        ):
            _uuid7(getattr(self, name), name)
        if type(self.intent) is not LockedOrderIntent:
            raise TypeError("exact locked KIS intent is required")
        if type(self.account) is not KisCashAccount:
            raise TypeError("exact KIS cash account is required")
        self.intent.__post_init__()
        self.account.__post_init__()
        if type(self.binding_generation) is not int or self.binding_generation <= 0:
            raise ValueError("binding_generation must be positive")
        if self.strategy_version != _STRATEGY_VERSION:
            raise ValueError("exact David v6 strategy version is required")
        if type(self.writer_capability) is not bool:
            raise TypeError("writer_capability must be bool")
        if self.target_order is not None:
            if type(self.target_order) is not ProviderOrderIdentity:
                raise TypeError("target_order must be exact")
            self.target_order.__post_init__()


class KisCashWriteAuthority(Protocol):
    async def load(self, command: BrokerOrderCommand) -> KisCashWriteContext: ...


class KisCashRecoverySource(Protocol):
    async def daily_orders(
        self,
        binding_id: UUID,
        provider_trade_date: date,
    ) -> tuple[KisDailyOrder, ...]: ...


class KisCashWriter:
    def __init__(
        self,
        *,
        authority: KisCashWriteAuthority,
        transport: KisCashOrderTransport,
        recovery: KisCashRecoverySource,
    ) -> None:
        self._authority = authority
        self._transport = transport
        self._recovery = recovery

    async def submit_locked(self, command: BrokerOrderCommand) -> BrokerWriteResult:
        context = await self._load_context(command, CommandType.SUBMIT)
        request = build_cash_order_request(context.intent, context.account)
        dispatch = await self._transport.dispatch_once(command.id, request)
        return _require_write_result(command, dispatch)

    async def cancel_locked(self, command: BrokerOrderCommand) -> BrokerWriteResult:
        context = await self._load_context(command, CommandType.CANCEL)
        if context.target_order is None:
            raise BrokerWriteDisabled("exact KIS cancel target authority is absent")
        expected_target = kis_provider_order_id(
            _provider_date(command),
            context.target_order.organization_number,
            context.target_order.order_number,
        )
        if command.target_broker_order_id != expected_target:
            raise BrokerWriteDisabled("KIS cancel target authority does not match")
        if command.quantity != context.target_order.remaining_quantity:
            raise BrokerWriteDisabled(
                "KIS cancel must cover the full remaining quantity"
            )
        request = build_cash_cancel_request(context.target_order, context.account)
        dispatch = await self._transport.dispatch_once(command.id, request)
        return _require_write_result(command, dispatch)

    async def submit(self, command: BrokerOrderCommand) -> BrokerWriteResult:
        return await self.submit_locked(command)

    async def cancel(self, command: BrokerOrderCommand) -> BrokerWriteResult:
        return await self.cancel_locked(command)

    async def replace(self, command: BrokerOrderCommand) -> BrokerWriteResult:
        del command
        raise BrokerWriteDisabled("KIS cash replace is not approved")

    async def recover_submit(
        self,
        command: BrokerOrderCommand,
        *,
        now: datetime,
    ) -> BrokerWriteResult | None:
        context = await self._load_context(command, CommandType.SUBMIT)
        _utc_second(now, "now")
        attempted_at = command.dispatch_attempted_at
        if attempted_at is None or not attempted_at <= now < command.not_after:
            return None
        request = build_cash_order_request(context.intent, context.account)
        dispatch = await self._transport.dispatch_once(command.id, request)
        if dispatch.state is KisDispatchState.ACKNOWLEDGED:
            return _require_write_result(command, dispatch)
        if dispatch.state is KisDispatchState.REJECTED:
            raise KisCashWriterRejected("KIS cash write was rejected")
        if dispatch.state is not KisDispatchState.AMBIGUOUS:
            return None
        provider_trade_date = attempted_at.astimezone(_KST).date()
        try:
            orders = await self._recovery.daily_orders(
                context.provider_binding_id,
                provider_trade_date,
            )
            decision = await recover_ambiguous_cash_order(
                KisAmbiguousDispatch(
                    dispatch_id=command.id,
                    binding_id=context.provider_binding_id,
                    side=command.side,
                    symbol=context.intent.symbol,
                    order_style=command.order_style,
                    quantity=command.quantity,
                    limit_price=command.limit_price,
                    provider_window_start=attempted_at,
                    provider_window_end=now,
                    request_digest=KisCashOrderTransport.request_digest(request),
                ),
                orders,
            )
        except Exception:
            return None
        if (
            decision.status is not KisRecoveryStatus.ADOPTED
            or decision.adopted_order is None
        ):
            return None
        order = decision.adopted_order
        return BrokerWriteResult(
            broker_order_id=kis_provider_order_id(
                order.order_date,
                order.organization_number,
                order.order_number,
            ),
            provider_state="RECOVERED",
        )

    async def _load_context(
        self,
        command: BrokerOrderCommand,
        expected_type: CommandType,
    ) -> KisCashWriteContext:
        if type(command) is not BrokerOrderCommand:
            raise TypeError("exact BrokerOrderCommand is required")
        if command.command_type is not expected_type:
            raise BrokerWriteDisabled("KIS cash command type is not approved")
        context = await self._authority.load(command)
        if type(context) is not KisCashWriteContext:
            raise BrokerWriteDisabled("exact KIS write authority is absent")
        try:
            context.__post_init__()
        except TypeError, ValueError:
            raise BrokerWriteDisabled("exact KIS write authority is invalid") from None
        if not context.writer_capability:
            raise BrokerWriteDisabled("KIS writer capability is absent")
        intent = context.intent
        attempted_at = command.dispatch_attempted_at
        if (
            context.command_id != command.id
            or context.instrument_id != command.instrument_id
            or context.account.account_id != command.account_id
            or intent.account_id != command.account_id
            or intent.binding_generation != context.binding_generation
            or command.fencing_token != context.binding_generation
            or not writes_to_a_venue(command.origin_type, command.authority_class)
            or command.side is not intent.side
            or command.order_style is not intent.order_style
            or command.quantity != intent.quantity
            or command.limit_price != intent.limit_price
            or command.time_in_force != "DAY"
            or attempted_at is None
            or not _is_utc_second(attempted_at)
            or not _is_utc_second(command.not_after)
            or attempted_at >= command.not_after
        ):
            raise BrokerWriteDisabled("exact KIS write authority does not match")
        if expected_type is CommandType.SUBMIT and context.target_order is not None:
            raise BrokerWriteDisabled("KIS submit cannot carry a provider target")
        return context


def _require_write_result(
    command: BrokerOrderCommand,
    dispatch: KisDispatchResult,
) -> BrokerWriteResult:
    if dispatch.state is KisDispatchState.REJECTED:
        raise KisCashWriterRejected("KIS cash write was rejected")
    if dispatch.state is not KisDispatchState.ACKNOWLEDGED:
        raise KisCashWriterUnknown("KIS cash write result is unknown")
    if dispatch.organization_number is None or dispatch.order_number is None:
        raise KisCashWriterUnknown("KIS cash acknowledgement identity is incomplete")
    return BrokerWriteResult(
        broker_order_id=kis_provider_order_id(
            _provider_date(command),
            dispatch.organization_number,
            dispatch.order_number,
        ),
        provider_state=dispatch.state.value,
    )


def _provider_date(command: BrokerOrderCommand) -> str:
    if command.dispatch_attempted_at is None:
        raise BrokerWriteDisabled("KIS dispatch time authority is absent")
    return command.dispatch_attempted_at.astimezone(_KST).strftime("%Y%m%d")


def kis_provider_order_id(order_date: str, organization: str, order_number: str) -> str:
    if (
        len(order_date) != 8
        or not order_date.isascii()
        or not order_date.isdecimal()
        or len(organization) != 5
        or not organization.isascii()
        or not organization.isdecimal()
        or len(order_number) != 10
        or not order_number.isascii()
        or not order_number.isdecimal()
    ):
        raise ValueError("KIS provider order identity is invalid")
    return f"KIS-KRX:{order_date}:{organization}:{order_number}"


def _uuid7(value: object, name: str) -> UUID:
    if type(value) is not UUID or value.version != 7:
        raise ValueError(f"{name} must be UUIDv7")
    return value


def _is_utc_second(value: object) -> bool:
    return (
        type(value) is datetime
        and value.tzinfo is not None
        and value.utcoffset() == UTC.utcoffset(value)
        and value.microsecond == 0
    )


def _utc_second(value: object, name: str) -> datetime:
    if not _is_utc_second(value):
        raise ValueError(f"{name} must be exact whole-second UTC")
    assert isinstance(value, datetime)
    return value


__all__ = (
    "BrokerWriteResult",
    "KisCashRecoverySource",
    "KisCashWriteAuthority",
    "KisCashWriteContext",
    "KisCashWriter",
    "KisCashWriterRejected",
    "KisCashWriterUnknown",
    "kis_provider_order_id",
)
