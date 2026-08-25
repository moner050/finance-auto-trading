from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from types import FrameType, SimpleNamespace, TracebackType
from typing import cast
from urllib.request import Request
from uuid import uuid7

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.common import (
    BrokerMarket,
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)
from autotrader.integrations.brokers.toss.market_data_reader import TossAccessToken
from autotrader.integrations.brokers.toss.stock_order_contracts import (
    TossStockOrderPreview,
    build_toss_stock_order_preview,
)
from autotrader.integrations.brokers.toss.submit_recovery import TossSubmitRecovery
from autotrader.integrations.brokers.toss.write_transport import (
    TossOrderWriteHttpsTransport,
)

ATTEMPTED_AT = datetime(2026, 8, 19, 1, 0, tzinfo=UTC)


@dataclass
class FakeHttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict[str, str])

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def read(self, amount: int = -1) -> bytes:
        del amount
        return self.body


@dataclass
class RecordingOpener:
    response: FakeHttpResponse
    requests: list[Request] = field(default_factory=list[Request])

    def __call__(self, request: Request, timeout: float) -> FakeHttpResponse:
        del timeout
        self.requests.append(request)
        return self.response


@dataclass
class RetainingFailureTransport:
    error: BaseException
    captured_request: BrokerRequest | None = None

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.captured_request = request
        raise self.error


@dataclass
class RaisingAccessToken:
    error: BaseException

    @property
    def value(self) -> str:
        raise self.error


def _command(*, not_after: datetime) -> BrokerOrderCommand:
    return BrokerOrderCommand(
        id=uuid7(),
        order_id=uuid7(),
        account_id=uuid7(),
        instrument_id=uuid7(),
        command_type=CommandType.SUBMIT,
        target_aggregate_version=1,
        idempotency_key=f"submit:{uuid7()}",
        command_sequence=1,
        canonical_payload_hash=b"c" * 32,
        broker_client_order_id=uuid7().hex,
        target_broker_order_id=None,
        replaces_command_id=None,
        origin_type="STRATEGY",
        authority_class="SUBMIT_NEW_EXPOSURE",
        owner_runtime_instance_id=uuid7(),
        fencing_token=1,
        not_after=not_after,
        side=Side.BUY,
        order_style=OrderStyle.LIMIT,
        quantity=Decimal("2"),
        limit_price=Decimal("70000"),
        time_in_force="DAY",
        status="UNKNOWN",
        dispatch_attempted_at=ATTEMPTED_AT,
    )


def _preview(command: BrokerOrderCommand) -> TossStockOrderPreview:
    return build_toss_stock_order_preview(
        command=command,
        account_seq=17,
        market=BrokerMarket.KRX_STOCK,
        symbol="005930",
        now=ATTEMPTED_AT,
    )


def _recovery(
    *, opener: RecordingOpener, command: BrokerOrderCommand
) -> TossSubmitRecovery:
    preview = _preview(command)
    return TossSubmitRecovery(
        transport=TossOrderWriteHttpsTransport(opener=opener),
        access_token=TossAccessToken(value="private-token", expires_in_seconds=3600),
        preview=preview,
        expected_body_sha256=sha256(preview.body).digest(),
    )


def _recovery_with_transport(
    *, transport: object, command: BrokerOrderCommand
) -> tuple[TossSubmitRecovery, TossAccessToken, TossStockOrderPreview]:
    preview = _preview(command)
    token = TossAccessToken(value="private-token", expires_in_seconds=3600)
    return (
        TossSubmitRecovery(
            transport=transport,  # type: ignore[arg-type]
            access_token=token,
            preview=preview,
            expected_body_sha256=sha256(preview.body).digest(),
        ),
        token,
        preview,
    )


@pytest.mark.asyncio
async def test_exact_replay_returns_the_matching_previous_order_result() -> None:
    command = _command(not_after=ATTEMPTED_AT + timedelta(minutes=5))
    opener = RecordingOpener(
        FakeHttpResponse(
            status=200,
            body=(
                b'{"result":{"orderId":"provider-order-1","clientOrderId":"'
                + command.broker_client_order_id.encode("ascii")
                + b'"}}'
            ),
        )
    )

    recovered = await _recovery(opener=opener, command=command).recover_submit(
        command,
        now=ATTEMPTED_AT + timedelta(seconds=1),
    )

    assert recovered is not None
    assert recovered.broker_order_id == "provider-order-1"
    assert len(opener.requests) == 1
    request = opener.requests[0]
    assert request.full_url == "https://openapi.tossinvest.com/api/v1/orders"
    assert request.method == "POST"
    assert request.data == _preview(command).body
    assert request.get_header("Authorization") == "Bearer private-token"
    assert request.get_header("X-tossinvest-account") == "17"
    assert request.get_header("Content-type") == "application/json"


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", ("command", "provider"))
async def test_replay_opens_nothing_at_either_exclusive_deadline(
    deadline: str,
) -> None:
    command_not_after = ATTEMPTED_AT + (
        timedelta(seconds=30) if deadline == "command" else timedelta(minutes=20)
    )
    command = _command(not_after=command_not_after)
    opener = RecordingOpener(FakeHttpResponse(status=200, body=b"{}"))
    now = (
        command_not_after
        if deadline == "command"
        else ATTEMPTED_AT + timedelta(seconds=600)
    )

    recovered = await _recovery(opener=opener, command=command).recover_submit(
        command,
        now=now,
    )

    assert recovered is None
    assert opener.requests == []


@pytest.mark.asyncio
async def test_forged_submit_shaped_object_never_reaches_transport() -> None:
    command = _command(not_after=ATTEMPTED_AT + timedelta(minutes=5))
    opener = RecordingOpener(FakeHttpResponse(status=200, body=b"{}"))
    forged = SimpleNamespace(
        command_type=SimpleNamespace(value="SUBMIT"),
        broker_client_order_id=command.broker_client_order_id,
        not_after=command.not_after,
        dispatch_attempted_at=command.dispatch_attempted_at,
    )

    recovered = await _recovery(opener=opener, command=command).recover_submit(
        cast(BrokerOrderCommand, forged),
        now=ATTEMPTED_AT + timedelta(seconds=1),
    )

    assert recovered is None
    assert opener.requests == []


@pytest.mark.asyncio
async def test_recovery_uses_the_constructor_snapshot_after_input_dto_mutation() -> (
    None
):
    command = _command(not_after=ATTEMPTED_AT + timedelta(minutes=5))
    opener = RecordingOpener(
        FakeHttpResponse(
            status=200,
            body=(
                b'{"result":{"orderId":"provider-order-1","clientOrderId":"'
                + command.broker_client_order_id.encode("ascii")
                + b'"}}'
            ),
        )
    )
    transport = TossOrderWriteHttpsTransport(opener=opener)
    recovery, token, preview = _recovery_with_transport(
        transport=transport,
        command=command,
    )
    original_body = preview.body
    object.__setattr__(token, "value", "changed-token")
    object.__setattr__(preview, "account_seq", "18")
    object.__setattr__(preview, "body", b'{"changed":true}')

    recovered = await recovery.recover_submit(
        command,
        now=ATTEMPTED_AT + timedelta(seconds=1),
    )

    assert recovered is not None
    assert opener.requests[0].data == original_body
    assert opener.requests[0].get_header("Authorization") == "Bearer private-token"
    assert opener.requests[0].get_header("X-tossinvest-account") == "17"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"),
    ((409, "request-in-progress"), (422, "idempotency-key-conflict")),
)
async def test_pending_or_conflicting_replay_stays_unknown_after_one_post(
    status: int, code: str
) -> None:
    command = _command(not_after=ATTEMPTED_AT + timedelta(minutes=5))
    opener = RecordingOpener(
        FakeHttpResponse(
            status=status,
            body=json.dumps({"error": {"code": code}}).encode("utf-8"),
        )
    )

    recovered = await _recovery(opener=opener, command=command).recover_submit(
        command,
        now=ATTEMPTED_AT + timedelta(seconds=1),
    )

    assert recovered is None
    assert len(opener.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body_kind", ("malformed", "wrong_client"))
async def test_invalid_success_acknowledgement_is_not_recovered(body_kind: str) -> None:
    command = _command(not_after=ATTEMPTED_AT + timedelta(minutes=5))
    body = (
        b"not-json"
        if body_kind == "malformed"
        else b'{"result":{"orderId":"provider-order-1","clientOrderId":"other"}}'
    )
    opener = RecordingOpener(FakeHttpResponse(status=200, body=body))

    recovered = await _recovery(opener=opener, command=command).recover_submit(
        command,
        now=ATTEMPTED_AT + timedelta(seconds=1),
    )

    assert recovered is None
    assert len(opener.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("command_type", (CommandType.CANCEL, CommandType.REPLACE))
async def test_cancel_and_replace_are_not_replayed(command_type: CommandType) -> None:
    submit = _command(not_after=ATTEMPTED_AT + timedelta(minutes=5))
    command = replace(submit, command_type=command_type)
    opener = RecordingOpener(FakeHttpResponse(status=200, body=b"{}"))

    recovered = await _recovery(opener=opener, command=submit).recover_submit(
        command,
        now=ATTEMPTED_AT + timedelta(seconds=1),
    )

    assert recovered is None
    assert opener.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broker_request",
    (
        BrokerRequest(method="GET", path="/api/v1/orders"),
        BrokerRequest(method="POST", path="/api/v1/orders?retry=1", body=b"{}"),
        BrokerRequest(method="POST", path="/api/v1/orders/", body=b"{}"),
        BrokerRequest(method="POST", path="/api/v1/orders/opaque", body=b"{}"),
        BrokerRequest(method="POST", path="/api/v1/buying-power", body=b"{}"),
    ),
)
async def test_write_transport_rejects_every_non_exact_order_create_route(
    broker_request: BrokerRequest,
) -> None:
    opener = RecordingOpener(FakeHttpResponse(status=200, body=b"{}"))
    transport = TossOrderWriteHttpsTransport(opener=opener)

    with pytest.raises(BrokerWriteDisabled, match="not allowed"):
        await transport.request(broker_request)

    assert opener.requests == []


def test_write_transport_has_no_default_network_opener() -> None:
    with pytest.raises(TypeError):
        TossOrderWriteHttpsTransport()  # type: ignore[call-arg]


def test_invalid_constructor_evidence_does_not_retain_sensitive_inputs() -> None:
    command = _command(not_after=ATTEMPTED_AT + timedelta(minutes=5))
    opener = RecordingOpener(FakeHttpResponse(status=200, body=b"{}"))
    transport = TossOrderWriteHttpsTransport(opener=opener)
    token = TossAccessToken(value="private-constructor-token", expires_in_seconds=3600)
    preview = _preview(command)
    forbidden_ids = frozenset(map(id, (transport, token, preview)))
    forbidden_content = frozenset(
        {
            "private-constructor-token",
            preview.account_seq,
            preview.client_order_id,
            preview.body.decode("utf-8"),
        }
    )

    try:
        TossSubmitRecovery(
            transport=transport,
            access_token=token,
            preview=preview,
            expected_body_sha256=b"x" * 32,
        )
    except ValueError as caught:
        error = caught
    else:
        pytest.fail("invalid recovery evidence must fail closed")
    del command, opener, transport, token, preview

    assert str(error) == "Toss submit recovery evidence is invalid"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert not _traceback_reaches_forbidden(
        error,
        forbidden_ids=forbidden_ids,
        forbidden_content=forbidden_content,
    )


def test_constructor_control_failure_is_sanitized_without_sensitive_inputs() -> None:
    command = _command(not_after=ATTEMPTED_AT + timedelta(minutes=5))
    opener = RecordingOpener(FakeHttpResponse(status=200, body=b"{}"))
    transport = TossOrderWriteHttpsTransport(opener=opener)
    error = SystemExit("private-constructor-control")
    token = RaisingAccessToken(error)
    preview = _preview(command)
    forbidden_ids = frozenset(map(id, (transport, token, preview)))
    forbidden_content = frozenset(
        {
            "private-constructor-control",
            preview.account_seq,
            preview.client_order_id,
            preview.body.decode("utf-8"),
        }
    )

    with pytest.raises(SystemExit) as raised:
        TossSubmitRecovery(
            transport=transport,
            access_token=token,
            preview=preview,
            expected_body_sha256=sha256(preview.body).digest(),
        )
    del command, opener, transport, token, preview

    assert raised.value is error
    assert error.args == ()
    assert error.code == 1
    assert error.__cause__ is None
    assert error.__context__ is None
    assert not _traceback_reaches_forbidden(
        error,
        forbidden_ids=forbidden_ids,
        forbidden_content=forbidden_content,
    )


def test_recovery_import_does_not_load_execution_or_runtime_modules() -> None:
    code = """
import importlib
import json
import sys

importlib.import_module('autotrader.integrations.brokers.toss.submit_recovery')
forbidden = (
    'autotrader.application',
    'autotrader.apps',
    'autotrader.execution',
    'autotrader.operations',
    'autotrader.persistence',
    'autotrader.risk',
    'autotrader.strategies',
)
print(json.dumps(sorted(
    name for name in sys.modules
    if any(name == prefix or name.startswith(prefix + '.') for prefix in forbidden)
)))
"""

    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == []


@pytest.mark.asyncio
async def test_ordinary_failure_does_not_retain_recovery_inputs_in_traceback() -> None:
    error, raised, forbidden_ids, forbidden_content = await _capture_failure(
        RuntimeError()
    )

    assert raised is False
    assert not _traceback_reaches_forbidden(
        error,
        forbidden_ids=forbidden_ids,
        forbidden_content=forbidden_content,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    (
        asyncio.CancelledError("private-control"),
        KeyboardInterrupt("private-control"),
        SystemExit("private-control"),
    ),
)
async def test_control_failure_propagates_sanitized_without_recovery_inputs(
    error: BaseException,
) -> None:
    caught, raised, forbidden_ids, forbidden_content = await _capture_failure(error)

    assert raised is True
    assert caught is error
    assert caught.args == ()
    assert caught.__cause__ is None
    assert caught.__context__ is None
    if isinstance(caught, SystemExit):
        assert caught.code == 1
    assert not _traceback_reaches_forbidden(
        caught,
        forbidden_ids=forbidden_ids,
        forbidden_content=forbidden_content,
    )


async def _capture_failure(
    error: BaseException,
) -> tuple[BaseException, bool, frozenset[int], frozenset[str]]:
    command = _command(not_after=ATTEMPTED_AT + timedelta(minutes=5))
    transport = RetainingFailureTransport(error)
    recovery, token, preview = _recovery_with_transport(
        transport=transport,
        command=command,
    )
    forbidden_ids = frozenset(map(id, (command, transport, recovery, token, preview)))
    forbidden_content = frozenset(
        {
            "private-token",
            command.broker_client_order_id,
            preview.account_seq,
            preview.body.decode("utf-8"),
        }
    )
    try:
        result = await recovery.recover_submit(
            command,
            now=ATTEMPTED_AT + timedelta(seconds=1),
        )
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as caught:
        del command, transport, recovery, token, preview
        return caught, True, forbidden_ids, forbidden_content
    assert result is None
    del command, transport, recovery, token, preview
    return error, False, forbidden_ids, forbidden_content


def _traceback_reaches_forbidden(
    error: BaseException,
    *,
    forbidden_ids: frozenset[int],
    forbidden_content: frozenset[str],
) -> bool:
    pending: list[object] = [error.__traceback__]
    seen: set[int] = set()
    while pending and len(seen) < 512:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if id(current) in forbidden_ids:
            return True
        if isinstance(current, str | bytes):
            text = current if isinstance(current, str) else repr(current)
            if any(secret in text for secret in forbidden_content):
                return True
        elif isinstance(current, BaseException):
            pending.extend(
                (
                    current.args,
                    current.__cause__,
                    current.__context__,
                    current.__traceback__,
                )
            )
        elif isinstance(current, TracebackType):
            pending.extend((current.tb_frame, current.tb_next))
        elif isinstance(current, FrameType):
            filename = current.f_code.co_filename.replace("\\", "/")
            if "/src/autotrader/" in filename:
                pending.extend(cast(dict[str, object], current.f_locals).values())
                if current.f_back is not None:
                    pending.append(current.f_back)
        elif isinstance(current, tuple | list | set | frozenset):
            pending.extend(
                cast(tuple[object, ...] | list[object] | set[object], current)
            )
        elif isinstance(current, dict):
            pending.extend(cast(dict[object, object], current).items())
        elif hasattr(current, "__dict__"):
            pending.append(vars(current))
        current_object = cast(object, current)
        slots = getattr(type(current_object), "__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for slot in cast(tuple[str, ...], slots):
            if hasattr(current_object, slot):
                pending.append(getattr(current_object, slot))
    return False
