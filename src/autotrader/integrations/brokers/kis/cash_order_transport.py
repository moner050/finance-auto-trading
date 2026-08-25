from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from autotrader.integrations.brokers.common import BrokerRequest, BrokerResponse
from autotrader.integrations.brokers.kis.cash_order_contracts import (
    KisCashOrderBusinessError,
    decode_cash_order_response,
)

_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "appkey",
        "appsecret",
        "hashkey",
        "personalseckey",
    }
)
_SAFE_ERROR_CLASSES = frozenset(
    {
        "PRE_SEND_CONNECT_FAILURE",
        "POST_SEND_TIMEOUT",
        "POST_SEND_RESET",
        "PROVIDER_BUSINESS_REJECTION",
        "MALFORMED_PROVIDER_RESPONSE",
        "HTTP_STATUS_CONFLICT",
        "UNCLASSIFIED_POST_PREPARE_FAILURE",
    }
)
_RESPONSE_ADAPTER = TypeAdapter(dict[str, object])


class KisDispatchState(StrEnum):
    PREPARED = "PREPARED"
    NOT_SENT = "NOT_SENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    AMBIGUOUS = "AMBIGUOUS"


class KisPostSendFailureKind(StrEnum):
    TIMEOUT = "TIMEOUT"
    CONNECTION_RESET = "CONNECTION_RESET"


class KisPreSendFailure(RuntimeError):
    """An exact failure proving that no request bytes reached KIS."""


class KisPostSendFailure(RuntimeError):
    """A failure after request bytes may have reached KIS."""

    def __init__(self, kind: KisPostSendFailureKind) -> None:
        if type(kind) is not KisPostSendFailureKind:
            raise TypeError("post-send failure kind must be exact")
        super().__init__(kind.value)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class KisDispatchRecord:
    dispatch_id: UUID
    request_digest: bytes
    state: KisDispatchState
    fencing_token: int
    attempt_count: int
    response_digest: bytes | None = None
    error_class: str | None = None
    organization_number: str | None = None
    order_number: str | None = None
    order_time: str | None = None
    message_code: str | None = None

    @classmethod
    def prepared(cls, dispatch_id: UUID, request_digest: bytes) -> KisDispatchRecord:
        record = cls(
            dispatch_id=dispatch_id,
            request_digest=request_digest,
            state=KisDispatchState.PREPARED,
            fencing_token=1,
            attempt_count=1,
        )
        record.validate()
        return record

    def validate(self) -> None:
        _require_uuid7(self.dispatch_id, "dispatch_id")
        _require_digest(self.request_digest, "request_digest")
        if type(self.state) is not KisDispatchState:
            raise TypeError("state must be an exact KisDispatchState")
        if type(self.fencing_token) is not int or self.fencing_token <= 0:
            raise ValueError("fencing_token must be positive")
        if type(self.attempt_count) is not int or self.attempt_count <= 0:
            raise ValueError("attempt_count must be positive")
        if self.response_digest is not None:
            _require_digest(self.response_digest, "response_digest")
        if self.error_class is not None and self.error_class not in _SAFE_ERROR_CLASSES:
            raise ValueError("dispatch error_class is not approved")
        identity = (
            self.organization_number,
            self.order_number,
            self.order_time,
            self.message_code,
        )
        if self.state is KisDispatchState.ACKNOWLEDGED:
            if (
                self.response_digest is None
                or not _digits(self.organization_number, 5)
                or not _digits(self.order_number, 10)
                or not _digits(self.order_time, 6)
                or not _safe_code(self.message_code)
                or self.error_class is not None
            ):
                raise ValueError("acknowledged dispatch evidence is incomplete")
        elif any(value is not None for value in identity):
            raise ValueError(
                "only acknowledged dispatches may retain provider identity"
            )
        if self.state is KisDispatchState.PREPARED and (
            self.response_digest is not None or self.error_class is not None
        ):
            raise ValueError("prepared dispatch cannot contain an outcome")
        if self.state is KisDispatchState.NOT_SENT and (
            self.response_digest is not None
            or self.error_class != "PRE_SEND_CONNECT_FAILURE"
        ):
            raise ValueError("not-sent dispatch requires exact pre-send evidence")
        if self.state is KisDispatchState.REJECTED and (
            self.response_digest is None
            or self.error_class != "PROVIDER_BUSINESS_REJECTION"
        ):
            raise ValueError("rejected dispatch requires provider response evidence")
        if self.state is KisDispatchState.AMBIGUOUS and self.error_class is None:
            raise ValueError("ambiguous dispatch requires a redacted classification")


@dataclass(frozen=True, slots=True)
class KisDispatchClaim:
    record: KisDispatchRecord
    acquired: bool

    def __post_init__(self) -> None:
        self.record.validate()
        if type(self.acquired) is not bool:
            raise TypeError("acquired must be bool")
        if self.acquired and self.record.state is not KisDispatchState.PREPARED:
            raise ValueError("only PREPARED dispatches may be acquired")


@dataclass(frozen=True, slots=True)
class KisDispatchResult:
    dispatch_id: UUID
    state: KisDispatchState
    fencing_token: int
    attempt_count: int
    response_digest: bytes | None
    error_class: str | None
    organization_number: str | None
    order_number: str | None
    order_time: str | None
    message_code: str | None

    @classmethod
    def from_record(cls, record: KisDispatchRecord) -> KisDispatchResult:
        record.validate()
        return cls(
            dispatch_id=record.dispatch_id,
            state=record.state,
            fencing_token=record.fencing_token,
            attempt_count=record.attempt_count,
            response_digest=record.response_digest,
            error_class=record.error_class,
            organization_number=record.organization_number,
            order_number=record.order_number,
            order_time=record.order_time,
            message_code=record.message_code,
        )


class KisDispatchStore(Protocol):
    async def prepare(
        self, dispatch_id: UUID, request_digest: bytes
    ) -> KisDispatchClaim: ...

    async def finish(
        self,
        dispatch_id: UUID,
        *,
        fencing_token: int,
        state: KisDispatchState,
        response_digest: bytes | None,
        error_class: str | None,
        organization_number: str | None,
        order_number: str | None,
        order_time: str | None,
        message_code: str | None,
    ) -> KisDispatchRecord: ...


class KisRequestSender(Protocol):
    async def request(self, request: BrokerRequest) -> BrokerResponse: ...


class KisCashOrderTransport:
    def __init__(self, store: KisDispatchStore, sender: KisRequestSender) -> None:
        self._store = store
        self._sender = sender

    @staticmethod
    def request_digest(request: BrokerRequest) -> bytes:
        if type(request) is not BrokerRequest:
            raise TypeError("request must be an exact BrokerRequest")
        headers = tuple(
            (name.lower(), value)
            for name, value in request.headers
            if name.lower() not in _SENSITIVE_HEADER_NAMES
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

    async def dispatch_once(
        self, dispatch_id: UUID, request: BrokerRequest
    ) -> KisDispatchResult:
        _require_uuid7(dispatch_id, "dispatch_id")
        digest = self.request_digest(request)
        claim = await self._store.prepare(dispatch_id, digest)
        if not claim.acquired:
            return KisDispatchResult.from_record(claim.record)
        token = claim.record.fencing_token
        try:
            response = await self._sender.request(request)
        except KisPreSendFailure:
            return await self._finish_result(
                dispatch_id,
                token,
                state=KisDispatchState.NOT_SENT,
                error_class="PRE_SEND_CONNECT_FAILURE",
            )
        except KisPostSendFailure as error:
            classification = (
                "POST_SEND_TIMEOUT"
                if error.kind is KisPostSendFailureKind.TIMEOUT
                else "POST_SEND_RESET"
            )
            return await self._finish_result(
                dispatch_id,
                token,
                state=KisDispatchState.AMBIGUOUS,
                error_class=classification,
            )
        except Exception:
            return await self._finish_result(
                dispatch_id,
                token,
                state=KisDispatchState.AMBIGUOUS,
                error_class="UNCLASSIFIED_POST_PREPARE_FAILURE",
            )
        return await self._finish_response(dispatch_id, token, response)

    async def _finish_response(
        self,
        dispatch_id: UUID,
        fencing_token: int,
        response: BrokerResponse,
    ) -> KisDispatchResult:
        response_digest = _response_digest(response)
        try:
            payload = _RESPONSE_ADAPTER.validate_json(response.body)
            acknowledgement = decode_cash_order_response(payload)
        except KisCashOrderBusinessError:
            return await self._finish_result(
                dispatch_id,
                fencing_token,
                state=KisDispatchState.REJECTED,
                response_digest=response_digest,
                error_class="PROVIDER_BUSINESS_REJECTION",
            )
        except ValidationError, ValueError, TypeError:
            return await self._finish_result(
                dispatch_id,
                fencing_token,
                state=KisDispatchState.AMBIGUOUS,
                response_digest=response_digest,
                error_class="MALFORMED_PROVIDER_RESPONSE",
            )
        if not 200 <= response.status < 300:
            return await self._finish_result(
                dispatch_id,
                fencing_token,
                state=KisDispatchState.AMBIGUOUS,
                response_digest=response_digest,
                error_class="HTTP_STATUS_CONFLICT",
            )
        return await self._finish_result(
            dispatch_id,
            fencing_token,
            state=KisDispatchState.ACKNOWLEDGED,
            response_digest=response_digest,
            organization_number=acknowledgement.organization_number,
            order_number=acknowledgement.order_number,
            order_time=acknowledgement.order_time,
            message_code=acknowledgement.message_code,
        )

    async def _finish_result(
        self,
        dispatch_id: UUID,
        fencing_token: int,
        *,
        state: KisDispatchState,
        response_digest: bytes | None = None,
        error_class: str | None = None,
        organization_number: str | None = None,
        order_number: str | None = None,
        order_time: str | None = None,
        message_code: str | None = None,
    ) -> KisDispatchResult:
        record = await self._store.finish(
            dispatch_id,
            fencing_token=fencing_token,
            state=state,
            response_digest=response_digest,
            error_class=error_class,
            organization_number=organization_number,
            order_number=order_number,
            order_time=order_time,
            message_code=message_code,
        )
        return KisDispatchResult.from_record(record)


def _response_digest(response: BrokerResponse) -> bytes:
    payload = (
        response.status.to_bytes(2, "big")
        + len(response.body).to_bytes(8, "big")
        + response.body
    )
    return sha256(payload).digest()


def _require_uuid7(value: object, name: str) -> UUID:
    if type(value) is not UUID or value.version != 7:
        raise ValueError(f"{name} must be UUIDv7")
    return value


def _require_digest(value: object, name: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        raise ValueError(f"{name} must be a 32-byte SHA-256 digest")
    return value


def _digits(value: object, length: int) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and value.isascii()
        and value.isdecimal()
    )


def _safe_code(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 32
        and value.isascii()
        and all(character.isalnum() or character in "_-" for character in value)
    )


__all__ = (
    "KisCashOrderTransport",
    "KisDispatchClaim",
    "KisDispatchRecord",
    "KisDispatchResult",
    "KisDispatchState",
    "KisDispatchStore",
    "KisPostSendFailure",
    "KisPostSendFailureKind",
    "KisPreSendFailure",
)
