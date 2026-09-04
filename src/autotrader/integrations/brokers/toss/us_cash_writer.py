from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from autotrader.domain.enums import Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.common import (
    BrokerMarket,
    BrokerRequest,
    BrokerResponse,
    BrokerSubmissionRejected,
    BrokerWriteDisabled,
    writes_to_a_venue,
)
from autotrader.integrations.brokers.toss.stock_order_contracts import (
    TossStockOrderPreview,
    TossStockOrderPreviewError,
    build_toss_stock_order_preview,
    decode_toss_order_submission_acknowledgement,
)
from autotrader.integrations.brokers.toss.submit_recovery import (
    TossPostSendFailure,
    TossPreSendFailure,
    TossRecoveryRecord,
    TossRecoveryState,
    TossRecoveryStore,
    canonical_toss_request_digest,
)

_STRATEGY_VERSION = "david-trullas-v6.0"
_PROVIDER_WINDOW = timedelta(seconds=600)


class TossUsCashWriterUnknown(RuntimeError):
    """Toss may have accepted the order; another ordinary submit is forbidden."""


class TossUsCashWriterNotSent(RuntimeError):
    """Exact transport evidence proves the first request was not sent."""


class TossUsCashWriterRejected(BrokerSubmissionRejected):
    """Toss authoritatively rejected the order."""


def toss_provider_order_id(provider_order_id: str) -> str:
    """The one place this string is built, so the reader matches the writer."""
    if not provider_order_id or provider_order_id != provider_order_id.strip():
        raise ValueError("Toss provider order id is invalid")
    return f"TOSS-US:{provider_order_id}"


@dataclass(frozen=True, slots=True)
class BrokerWriteResult:
    broker_order_id: str
    provider_state: str

    def __post_init__(self) -> None:
        if not self.broker_order_id.startswith("TOSS-US:"):
            raise ValueError("canonical Toss US broker order identity is required")
        if self.provider_state not in {"ACKNOWLEDGED", "RECOVERED"}:
            raise ValueError("Toss US provider state is invalid")


@dataclass(frozen=True, slots=True)
class TossUsCashWriteContext:
    command_id: UUID
    instrument_id: UUID
    account_id: UUID
    provider_binding_id: UUID
    binding_generation: int
    policy_version_id: UUID
    strategy_version: str
    writer_capability: bool
    account_enabled: bool
    binding_active: bool
    account_seq: int
    symbol: str
    intent_locked: bool
    opens_exposure: bool
    authorized_sellable_quantity: Decimal

    def __post_init__(self) -> None:
        for name in (
            "command_id",
            "instrument_id",
            "account_id",
            "provider_binding_id",
            "policy_version_id",
        ):
            _uuid7(getattr(self, name), name)
        if type(self.binding_generation) is not int or self.binding_generation <= 0:
            raise ValueError("binding_generation must be positive")
        if self.strategy_version != _STRATEGY_VERSION:
            raise ValueError("exact David v6 strategy version is required")
        for name in (
            "writer_capability",
            "account_enabled",
            "binding_active",
            "intent_locked",
            "opens_exposure",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")
        if self.account_enabled:
            raise ValueError("Toss US contract requires the exact disabled account")
        if type(self.account_seq) is not int or self.account_seq <= 0:
            raise ValueError("account_seq must be positive")
        if (
            type(self.symbol) is not str
            or not self.symbol
            or self.symbol != self.symbol.upper()
        ):
            raise ValueError("canonical uppercase US symbol is required")
        if type(self.authorized_sellable_quantity) is not Decimal:
            raise TypeError("authorized_sellable_quantity must be Decimal")
        if self.authorized_sellable_quantity < 0:
            raise ValueError("authorized_sellable_quantity cannot be negative")


@dataclass(frozen=True, slots=True)
class TossUsCashRecoveryContext:
    command: BrokerOrderCommand
    write_context: TossUsCashWriteContext

    def __post_init__(self) -> None:
        if type(self.command) is not BrokerOrderCommand:
            raise TypeError("exact recovery command is required")
        if type(self.write_context) is not TossUsCashWriteContext:
            raise TypeError("exact recovery write context is required")


class TossUsCashWriteAuthority(Protocol):
    async def load(self, command: BrokerOrderCommand) -> TossUsCashWriteContext: ...

    async def load_recovery(self, dispatch_id: UUID) -> TossUsCashRecoveryContext: ...


class TossUsAccessTokenSource(Protocol):
    async def load(self, binding_id: UUID) -> str: ...


class TossUsOrderSender(Protocol):
    async def request(self, request: BrokerRequest) -> BrokerResponse: ...


class TossUsCashWriter:
    def __init__(
        self,
        *,
        authority: TossUsCashWriteAuthority,
        token_source: TossUsAccessTokenSource,
        store: TossRecoveryStore,
        sender: TossUsOrderSender,
        clock: Callable[[], datetime],
        lease_owner: UUID,
    ) -> None:
        _uuid7(lease_owner, "lease_owner")
        self._authority = authority
        self._token_source = token_source
        self._store = store
        self._sender = sender
        self._clock = clock
        self._lease_owner = lease_owner

    async def submit_locked(self, command: BrokerOrderCommand) -> BrokerWriteResult:
        now = _utc_second(self._clock(), "now")
        context = await self._load_context(command, now=now, first_submit=True)
        request, preview = await self._request(command, context, now=now)
        attempted_at = _utc_second(
            command.dispatch_attempted_at,
            "dispatch_attempted_at",
        )
        expires_at = min(
            command.not_after,
            attempted_at + _PROVIDER_WINDOW,
        )
        record = TossRecoveryRecord(
            dispatch_id=command.id,
            binding_id=context.provider_binding_id,
            account_id=context.account_id,
            client_order_id=command.broker_client_order_id,
            first_dispatch_at=attempted_at,
            request_digest=canonical_toss_request_digest(request),
            lease_owner=self._lease_owner,
            lease_acquired_at=now,
            lease_expires_at=expires_at,
            replay_count=0,
            state=TossRecoveryState.OPEN,
        )
        record.validate()
        claim = await self._store.prepare(record)
        if not claim.acquired:
            return self._existing_result(claim.record)
        try:
            response = await self._sender.request(request)
        except TossPreSendFailure:
            raise TossUsCashWriterNotSent("Toss US order was not sent") from None
        except TossPostSendFailure, Exception:
            raise TossUsCashWriterUnknown("Toss US order result is unknown") from None
        return await self._finish_response(
            command.id,
            preview=preview,
            response=response,
            now=now,
            recovered=False,
        )

    async def recover(self, dispatch_id: UUID) -> BrokerWriteResult:
        _uuid7(dispatch_id, "dispatch_id")
        persisted = await self._store.load(dispatch_id)
        if persisted is None:
            raise TossUsCashWriterUnknown("Toss US recovery evidence is absent")
        if persisted.state is not TossRecoveryState.OPEN:
            return self._existing_result(persisted)
        recovery = await self._authority.load_recovery(dispatch_id)
        if type(recovery) is not TossUsCashRecoveryContext:
            raise BrokerWriteDisabled("exact Toss US recovery authority is absent")
        try:
            recovery.__post_init__()
        except TypeError, ValueError:
            raise BrokerWriteDisabled(
                "exact Toss US recovery authority is invalid"
            ) from None
        now = _utc_second(self._clock(), "now")
        command = recovery.command
        context = await self._validate_context(
            command,
            recovery.write_context,
            now=now,
            first_submit=False,
        )
        request, preview = await self._request(command, context, now=now)
        digest = canonical_toss_request_digest(request)
        if (
            persisted.binding_id != context.provider_binding_id
            or persisted.account_id != context.account_id
            or persisted.client_order_id != command.broker_client_order_id
            or persisted.first_dispatch_at != command.dispatch_attempted_at
            or persisted.request_digest != digest
        ):
            raise BrokerWriteDisabled("Toss US recovery request digest does not match")
        claim = await self._store.claim_replay(
            dispatch_id,
            lease_owner=self._lease_owner,
            now=now,
            request_digest=digest,
        )
        if not claim.acquired:
            return self._existing_result(claim.record)
        try:
            response = await self._sender.request(request)
            acknowledgement = decode_toss_order_submission_acknowledgement(
                response,
                preview=preview,
            )
        except Exception:
            await self._store.finish(
                dispatch_id,
                lease_owner=self._lease_owner,
                state=TossRecoveryState.UNKNOWN,
                terminal_at=now,
                provider_order_id=None,
            )
            raise TossUsCashWriterUnknown(
                "Toss US recovery result is unknown"
            ) from None
        await self._store.finish(
            dispatch_id,
            lease_owner=self._lease_owner,
            state=TossRecoveryState.ACKNOWLEDGED,
            terminal_at=now,
            provider_order_id=acknowledgement.order_id,
        )
        return _result(acknowledgement.order_id, recovered=True)

    async def _load_context(
        self,
        command: BrokerOrderCommand,
        *,
        now: datetime,
        first_submit: bool,
    ) -> TossUsCashWriteContext:
        if type(command) is not BrokerOrderCommand:
            raise TypeError("exact BrokerOrderCommand is required")
        context = await self._authority.load(command)
        return await self._validate_context(
            command,
            context,
            now=now,
            first_submit=first_submit,
        )

    async def _validate_context(
        self,
        command: BrokerOrderCommand,
        context: TossUsCashWriteContext,
        *,
        now: datetime,
        first_submit: bool,
    ) -> TossUsCashWriteContext:
        if type(command) is not BrokerOrderCommand:
            raise TypeError("exact BrokerOrderCommand is required")
        if command.command_type is not CommandType.SUBMIT:
            raise BrokerWriteDisabled("Toss US cash supports submit only")
        if type(context) is not TossUsCashWriteContext:
            raise BrokerWriteDisabled("exact Toss US write authority is absent")
        try:
            context.__post_init__()
        except TypeError, ValueError:
            raise BrokerWriteDisabled(
                "exact Toss US write authority is invalid"
            ) from None
        attempted_at = command.dispatch_attempted_at
        if not _is_utc_second(attempted_at) or not _is_utc_second(command.not_after):
            raise BrokerWriteDisabled(
                "exact Toss US write time authority does not match"
            )
        assert isinstance(attempted_at, datetime)
        if (
            not context.writer_capability
            or not context.binding_active
            or not context.intent_locked
        ):
            reason = (
                "capability"
                if not context.writer_capability
                else "binding or locked intent"
            )
            raise BrokerWriteDisabled(f"Toss US writer {reason} is absent")
        if (
            context.command_id != command.id
            or context.instrument_id != command.instrument_id
            or context.account_id != command.account_id
            or context.binding_generation != command.fencing_token
            or not writes_to_a_venue(command.origin_type, command.authority_class)
            or command.owner_runtime_instance_id is None
            or command.owner_runtime_instance_id.version != 7
            or command.target_broker_order_id is not None
            or command.replaces_command_id is not None
            or command.time_in_force not in {"DAY", "CLS"}
            or attempted_at > now
            or now >= command.not_after
            or (first_submit and now >= attempted_at + _PROVIDER_WINDOW)
        ):
            raise BrokerWriteDisabled("exact Toss US write authority does not match")
        if command.side is Side.BUY and not context.opens_exposure:
            raise BrokerWriteDisabled("Toss US buy exposure authority is absent")
        if command.side is Side.SELL and (
            context.opens_exposure
            or command.quantity > context.authorized_sellable_quantity
        ):
            raise BrokerWriteDisabled("Toss US short-opening sell is forbidden")
        return context

    async def _request(
        self,
        command: BrokerOrderCommand,
        context: TossUsCashWriteContext,
        *,
        now: datetime,
    ) -> tuple[BrokerRequest, TossStockOrderPreview]:
        preview = build_toss_stock_order_preview(
            command=command,
            account_seq=context.account_seq,
            market=BrokerMarket.US_STOCK,
            symbol=context.symbol,
            now=now,
        )
        token = await self._token_source.load(context.provider_binding_id)
        if type(token) is not str or not token or "\n" in token:
            raise BrokerWriteDisabled("Toss US access token is unavailable")
        return (
            BrokerRequest(
                method="POST",
                path="/api/v1/orders",
                headers=(
                    ("Authorization", f"Bearer {token}"),
                    ("Content-Type", "application/json"),
                    ("X-Tossinvest-Account", preview.account_seq),
                ),
                body=preview.body,
            ),
            preview,
        )

    async def _finish_response(
        self,
        dispatch_id: UUID,
        *,
        preview: TossStockOrderPreview,
        response: BrokerResponse,
        now: datetime,
        recovered: bool,
    ) -> BrokerWriteResult:
        try:
            acknowledgement = decode_toss_order_submission_acknowledgement(
                response,
                preview=preview,
            )
        except TossStockOrderPreviewError:
            raise TossUsCashWriterUnknown("Toss US order result is unknown") from None
        await self._store.finish(
            dispatch_id,
            lease_owner=self._lease_owner,
            state=TossRecoveryState.ACKNOWLEDGED,
            terminal_at=now,
            provider_order_id=acknowledgement.order_id,
        )
        return _result(acknowledgement.order_id, recovered=recovered)

    @staticmethod
    def _existing_result(record: TossRecoveryRecord) -> BrokerWriteResult:
        record.validate()
        if record.state is TossRecoveryState.ACKNOWLEDGED:
            assert record.provider_order_id is not None
            return _result(record.provider_order_id, recovered=True)
        if record.state is TossRecoveryState.REJECTED:
            raise TossUsCashWriterRejected("Toss US order was rejected")
        if record.state in {TossRecoveryState.UNKNOWN, TossRecoveryState.EXPIRED}:
            raise TossUsCashWriterUnknown("Toss US recovery is terminal")
        raise TossUsCashWriterUnknown("Toss US order recovery is already owned")


def _result(provider_order_id: str, *, recovered: bool) -> BrokerWriteResult:
    return BrokerWriteResult(
        broker_order_id=toss_provider_order_id(provider_order_id),
        provider_state="RECOVERED" if recovered else "ACKNOWLEDGED",
    )


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
    "TossUsCashRecoveryContext",
    "TossUsCashWriteAuthority",
    "TossUsCashWriteContext",
    "TossUsCashWriter",
    "TossUsCashWriterNotSent",
    "TossUsCashWriterRejected",
    "TossUsCashWriterUnknown",
)
