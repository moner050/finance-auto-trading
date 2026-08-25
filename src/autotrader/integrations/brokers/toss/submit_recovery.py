from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Protocol, TypeIs
from uuid import UUID

from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerRequest,
)
from autotrader.integrations.brokers.toss.stock_order_contracts import (
    TossStockOrderPreview,
    decode_toss_order_submission_acknowledgement,
)

_IDEMPOTENCY_WINDOW = timedelta(seconds=600)
_VOLATILE_HEADERS = frozenset({"authorization"})


class TossRecoveryState(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    EXPIRED = "EXPIRED"


class TossPostSendFailureKind(StrEnum):
    TIMEOUT = "TIMEOUT"
    RESET = "RESET"


class TossPreSendFailure(RuntimeError):
    """Exact evidence that no request bytes reached Toss."""


class TossPostSendFailure(RuntimeError):
    """The request may have reached Toss before transport failure."""

    def __init__(self, kind: TossPostSendFailureKind) -> None:
        if type(kind) is not TossPostSendFailureKind:
            raise TypeError("post-send failure kind must be exact")
        super().__init__(kind.value)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class TossRecoveryRecord:
    dispatch_id: UUID
    binding_id: UUID
    account_id: UUID
    client_order_id: str
    first_dispatch_at: datetime
    request_digest: bytes
    lease_owner: UUID
    lease_acquired_at: datetime
    lease_expires_at: datetime
    replay_count: int
    state: TossRecoveryState
    terminal_at: datetime | None = None
    provider_order_id: str | None = None

    def validate(self) -> None:
        for name in ("dispatch_id", "binding_id", "account_id", "lease_owner"):
            value = getattr(self, name)
            if type(value) is not UUID or value.version != 7:
                raise ValueError(f"{name} must be UUIDv7")
        if (
            type(self.client_order_id) is not str
            or not 1 <= len(self.client_order_id) <= 36
            or not self.client_order_id.isascii()
        ):
            raise ValueError("client_order_id must be short ASCII")
        if type(self.request_digest) is not bytes or len(self.request_digest) != 32:
            raise ValueError("request_digest must be SHA-256 bytes")
        for name in (
            "first_dispatch_at",
            "lease_acquired_at",
            "lease_expires_at",
        ):
            _require_utc_second(getattr(self, name), name)
        if type(self.state) is not TossRecoveryState:
            raise TypeError("state must be an exact TossRecoveryState")
        if type(self.replay_count) is not int or not 0 <= self.replay_count <= 1:
            raise ValueError("replay_count must be zero or one")
        if (
            self.lease_acquired_at < self.first_dispatch_at
            or self.lease_expires_at <= self.lease_acquired_at
            or self.lease_expires_at > self.first_dispatch_at + _IDEMPOTENCY_WINDOW
        ):
            raise ValueError("Toss recovery lease window is invalid")
        if self.state is TossRecoveryState.OPEN:
            if self.terminal_at is not None or self.provider_order_id is not None:
                raise ValueError("open recovery cannot have terminal evidence")
        else:
            terminal_at = _require_utc_second(self.terminal_at, "terminal_at")
            if terminal_at < self.first_dispatch_at:
                raise ValueError("terminal_at precedes dispatch")
            if self.state is TossRecoveryState.ACKNOWLEDGED:
                _require_provider_order_id(self.provider_order_id)
            elif self.provider_order_id is not None:
                raise ValueError("only acknowledged recovery has provider identity")


@dataclass(frozen=True, slots=True)
class TossRecoveryClaim:
    record: TossRecoveryRecord
    acquired: bool

    def __post_init__(self) -> None:
        self.record.validate()
        if type(self.acquired) is not bool:
            raise TypeError("acquired must be bool")
        if self.acquired and self.record.state is not TossRecoveryState.OPEN:
            raise ValueError("only open recovery may be acquired")


class TossRecoveryStore(Protocol):
    async def prepare(self, record: TossRecoveryRecord) -> TossRecoveryClaim: ...

    async def load(self, dispatch_id: UUID) -> TossRecoveryRecord | None: ...

    async def claim_replay(
        self,
        dispatch_id: UUID,
        *,
        lease_owner: UUID,
        now: datetime,
        request_digest: bytes,
    ) -> TossRecoveryClaim: ...

    async def finish(
        self,
        dispatch_id: UUID,
        *,
        lease_owner: UUID,
        state: TossRecoveryState,
        terminal_at: datetime,
        provider_order_id: str | None,
    ) -> TossRecoveryRecord: ...


def canonical_toss_request_digest(request: BrokerRequest) -> bytes:
    """Hash the exact replay contract without retaining volatile authorization."""
    if type(request) is not BrokerRequest:
        raise TypeError("request must be an exact BrokerRequest")
    headers = tuple(
        (name.lower(), value)
        for name, value in request.headers
        if name.lower() not in _VOLATILE_HEADERS
    )
    canonical = json.dumps(
        {
            "method": request.method,
            "path": request.path,
            "headers": headers,
            "body_sha256": sha256(request.body or b"").hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return sha256(canonical).digest()


def _require_utc_second(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
        or value.microsecond != 0
    ):
        raise ValueError(f"{name} must be exact whole-second UTC")
    return value


def _require_provider_order_id(value: object) -> str:
    if type(value) is not str or not value or "\n" in value:
        raise ValueError("provider_order_id is invalid")
    return value


class _AccessToken(Protocol):
    @property
    def value(self) -> str: ...


class _AttemptedSubmitCommand(Protocol):
    @property
    def command_type(self) -> object: ...

    @property
    def broker_client_order_id(self) -> str: ...

    @property
    def not_after(self) -> datetime: ...

    @property
    def dispatch_attempted_at(self) -> datetime | None: ...


@dataclass(frozen=True, slots=True)
class TossRecoveredSubmission:
    broker_order_id: str

    def __post_init__(self) -> None:
        if not self.broker_order_id or "\n" in self.broker_order_id:
            raise ValueError("Toss recovered broker order id is invalid")


class TossSubmitRecovery:
    """Replays one exact durable Toss submit inside its provider key window."""

    def __init__(
        self,
        *,
        transport: AsyncHttpTransport,
        access_token: _AccessToken,
        preview: TossStockOrderPreview,
        expected_body_sha256: bytes,
    ) -> None:
        snapshot = _snapshot_recovery_inputs(
            access_token=access_token,
            preview=preview,
            expected_body_sha256=expected_body_sha256,
        )
        if isinstance(snapshot, BaseException):
            error = snapshot
            snapshot = None
            del transport, access_token, preview, expected_body_sha256
            _scrub_control_exception(error)
            raise error from None
        if snapshot is None:
            del transport, access_token, preview, expected_body_sha256
            raise ValueError("Toss submit recovery evidence is invalid") from None
        token_value, account_seq, client_order_id, body, body_sha256 = snapshot
        self._transport = transport
        self._token_value = token_value
        self._account_seq = account_seq
        self._client_order_id = client_order_id
        self._body = body
        self._expected_body_sha256 = body_sha256

    async def recover_submit(
        self, command: _AttemptedSubmitCommand, *, now: datetime
    ) -> TossRecoveredSubmission | None:
        outcome = await self._recover_outcome(command=command, now=now)
        del self, command, now
        if isinstance(outcome, BaseException):
            error = outcome
            outcome = None
            _scrub_control_exception(error)
            raise error from None
        return outcome

    async def _recover_outcome(
        self, *, command: _AttemptedSubmitCommand, now: datetime
    ) -> TossRecoveredSubmission | BaseException | None:
        try:
            return await self._recover_submit(command=command, now=now)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as caught:
            _scrub_control_exception(caught)
            return caught
        except Exception as caught:
            _scrub_exception(caught)
            return None

    async def _recover_submit(
        self, *, command: _AttemptedSubmitCommand, now: datetime
    ) -> TossRecoveredSubmission | None:
        attempted_at = command.dispatch_attempted_at
        if (
            not _is_exact_command(command)
            or getattr(command.command_type, "value", None) != "SUBMIT"
            or not _is_exact_utc(now)
            or not _is_exact_utc(command.not_after)
            or not _is_exact_utc(attempted_at)
            or attempted_at > now
            or now >= command.not_after
            or now >= attempted_at + _IDEMPOTENCY_WINDOW
        ):
            return None
        preview = TossStockOrderPreview(
            account_seq=self._account_seq,
            client_order_id=self._client_order_id,
            body=self._body,
        )
        if (
            preview.client_order_id != command.broker_client_order_id
            or sha256(preview.body).digest() != self._expected_body_sha256
        ):
            return None
        token_value = self._token_value
        if not token_value or "\n" in token_value:
            return None
        response = await self._transport.request(
            BrokerRequest(
                method="POST",
                path="/api/v1/orders",
                headers=(
                    ("Authorization", f"Bearer {token_value}"),
                    ("Content-Type", "application/json"),
                    ("X-Tossinvest-Account", preview.account_seq),
                ),
                body=preview.body,
            )
        )
        if response.status in {409, 422}:
            return None
        acknowledgement = decode_toss_order_submission_acknowledgement(
            response, preview=preview
        )
        return TossRecoveredSubmission(acknowledgement.order_id)


def _is_exact_utc(value: object) -> TypeIs[datetime]:
    return (
        type(value) is datetime
        and value.tzinfo is UTC
        and value.utcoffset() == UTC.utcoffset(value)
    )


def _snapshot_recovery_inputs(
    *,
    access_token: _AccessToken,
    preview: TossStockOrderPreview,
    expected_body_sha256: bytes,
) -> tuple[str, str, str, bytes, bytes] | BaseException | None:
    try:
        if (
            type(preview) is not TossStockOrderPreview
            or type(expected_body_sha256) is not bytes
            or len(expected_body_sha256) != 32
            or sha256(preview.body).digest() != expected_body_sha256
        ):
            return None
        token_value = access_token.value
        if not token_value or "\n" in token_value:
            return None
        return (
            token_value,
            preview.account_seq,
            preview.client_order_id,
            bytes(preview.body),
            expected_body_sha256,
        )
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as caught:
        _scrub_control_exception(caught)
        return caught
    except Exception as caught:
        _scrub_exception(caught)
        return None


def _is_exact_command(value: object) -> TypeIs[_AttemptedSubmitCommand]:
    module = sys.modules.get("autotrader.execution.orders.models")
    command_type = (
        None if module is None else getattr(module, "BrokerOrderCommand", None)
    )
    return isinstance(command_type, type) and type(value) is command_type


def _scrub_exception(caught: BaseException) -> None:
    caught.__traceback__ = None
    caught.__context__ = None
    caught.__cause__ = None
    caught.args = ()
    caught.__dict__.clear()


def _scrub_control_exception(caught: BaseException) -> None:
    _scrub_exception(caught)
    if isinstance(caught, SystemExit):
        caught.code = 1
