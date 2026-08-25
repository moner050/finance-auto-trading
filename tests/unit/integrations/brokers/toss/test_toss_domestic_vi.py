from __future__ import annotations

import ast
import reprlib
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import FrameType, FunctionType, MethodType, TracebackType
from typing import cast

import pytest

from autotrader.integrations.brokers.common import (
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)
from autotrader.integrations.brokers.toss.domestic_vi import (
    TossDomesticViReadOnlyAdapter,
    TossIncompleteKrxViSnapshot,
    TossKrxViEvidence,
    TossKrxViWarning,
)


@dataclass
class ScriptedTransport:
    responses: list[BrokerResponse | BaseException]
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        outcome = self.responses.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _response(result: bytes) -> BrokerResponse:
    return BrokerResponse(status=200, body=b'{"result":' + result + b"}")


@pytest.mark.asyncio
async def test_reads_documented_krx_warning_endpoint_without_account_scope() -> None:
    transport = ScriptedTransport(responses=[_response(b"[]")])
    adapter = TossDomesticViReadOnlyAdapter(transport=transport)

    evidence = await adapter.read_krx_vi_evidence(
        symbol="005930", access_token="access-token"
    )

    assert evidence == TossKrxViEvidence(warnings=())
    assert evidence.has_active_krx_vi is False
    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path="/api/v1/stocks/005930/warnings",
            headers=(("Authorization", "Bearer access-token"),),
        )
    ]


@pytest.mark.asyncio
async def test_retains_warning_order_and_derives_only_documented_krx_vi_codes() -> None:
    transport = ScriptedTransport(
        responses=[
            _response(
                b"["
                b'{"warningType":"VI_STATIC","exchange":"KRX",'
                b'"startDate":"2026-08-01","endDate":"2026-08-02"},'
                b'{"warningType":"VI_DYNAMIC","exchange":"KRX",'
                b'"startDate":"2026-08-03","endDate":null},'
                b'{"warningType":"VI_STATIC_AND_DYNAMIC","exchange":"KRX",'
                b'"startDate":"2026-08-04","endDate":"2026-08-04"},'
                b'{"warningType":"VI_STATIC","exchange":"NXT",'
                b'"startDate":"2026-08-05","endDate":null},'
                b'{"warningType":"VI_DYNAMIC","exchange":null,'
                b'"startDate":"2026-08-06","endDate":null},'
                b'{"warningType":"INVESTMENT_WARNING","exchange":"KRX",'
                b'"startDate":"2026-08-07","endDate":null},'
                b'{"warningType":"FUTURE_PROVIDER_CODE","exchange":"KRX",'
                b'"startDate":"2026-08-08","endDate":null}'
                b"]"
            )
        ]
    )

    evidence = await TossDomesticViReadOnlyAdapter(
        transport=transport
    ).read_krx_vi_evidence(symbol="005930", access_token="access-token")

    assert evidence.warnings == (
        TossKrxViWarning("VI_STATIC", "KRX", date(2026, 8, 1), date(2026, 8, 2)),
        TossKrxViWarning("VI_DYNAMIC", "KRX", date(2026, 8, 3), None),
        TossKrxViWarning(
            "VI_STATIC_AND_DYNAMIC", "KRX", date(2026, 8, 4), date(2026, 8, 4)
        ),
        TossKrxViWarning("VI_STATIC", "NXT", date(2026, 8, 5), None),
        TossKrxViWarning("VI_DYNAMIC", None, date(2026, 8, 6), None),
        TossKrxViWarning("INVESTMENT_WARNING", "KRX", date(2026, 8, 7), None),
        TossKrxViWarning("FUTURE_PROVIDER_CODE", "KRX", date(2026, 8, 8), None),
    )
    assert [warning.is_krx_vi_warning for warning in evidence.warnings] == [
        True,
        True,
        True,
        False,
        False,
        False,
        False,
    ]
    assert evidence.has_active_krx_vi is True


@pytest.mark.asyncio
async def test_retains_canonical_optional_warning_fields_as_none() -> None:
    transport = ScriptedTransport(
        responses=[
            _response(
                b"["
                b'{"warningType":"VI_STATIC"},'
                b'{"warningType":"VI_DYNAMIC","exchange":null,'
                b'"startDate":null,"endDate":null},'
                b'{"warningType":"VI_STATIC_AND_DYNAMIC","exchange":"KRX",'
                b'"startDate":null,"endDate":"2026-08-03"}'
                b"]"
            )
        ]
    )

    evidence = await TossDomesticViReadOnlyAdapter(
        transport=transport
    ).read_krx_vi_evidence(symbol="005930", access_token="access-token")

    assert evidence.warnings == (
        TossKrxViWarning("VI_STATIC", None, None, None),
        TossKrxViWarning("VI_DYNAMIC", None, None, None),
        TossKrxViWarning("VI_STATIC_AND_DYNAMIC", "KRX", None, date(2026, 8, 3)),
    )
    assert [warning.is_krx_vi_warning for warning in evidence.warnings] == [
        False,
        False,
        True,
    ]
    assert evidence.has_active_krx_vi is True


@pytest.mark.parametrize(
    "response",
    [
        BrokerResponse(status=status, body=b'{"result":[]}')
        for status in (400, 404, 429, 500)
    ]
    + [
        BrokerResponse(status=200, body=body)
        for body in (
            b"not-json",
            b"[]",
            b'{"result":{}}',
            b'{"result":[null]}',
            b'{"result":[{"warningType":"","exchange":"KRX",'
            b'"startDate":"2026-08-01","endDate":null}]}',
            b'{"result":[{"warningType":"VI_STATIC","exchange":17,'
            b'"startDate":"2026-08-01","endDate":null}]}',
            b'{"result":[{"warningType":"VI_STATIC","exchange":"KRX",'
            b'"startDate":"2026/08/01","endDate":null}]}',
            b'{"result":[{"warningType":"VI_STATIC","exchange":"KRX",'
            b'"startDate":"2026-08-02","endDate":"2026-08-01"}]}',
            b'{"result":[{"warningType":"VI_STATIC","exchange":"",'
            b'"startDate":null,"endDate":null}]}',
            b'{"result":[{"warningType":"VI_STATIC","exchange":"KRX",'
            b'"startDate":17,"endDate":null}]}',
            b'{"result":[{"warningType":"VI_STATIC","exchange":"KRX",'
            b'"startDate":null,"endDate":17}]}',
        )
    ],
)
@pytest.mark.asyncio
async def test_rejects_incomplete_provider_snapshots(response: BrokerResponse) -> None:
    transport = ScriptedTransport(responses=[response])
    adapter = TossDomesticViReadOnlyAdapter(transport=transport)

    with pytest.raises(TossIncompleteKrxViSnapshot) as raised:
        await adapter.read_krx_vi_evidence(symbol="005930", access_token="access-token")

    assert str(raised.value) == "Toss KRX VI snapshot is incomplete"
    assert transport.requests and transport.requests[0].path.endswith("/warnings")


@pytest.mark.asyncio
async def test_transport_failure_is_incomplete_snapshot_after_one_request() -> None:
    transport = ScriptedTransport(responses=[OSError("synthetic transport failure")])

    with pytest.raises(TossIncompleteKrxViSnapshot, match="snapshot is incomplete"):
        await TossDomesticViReadOnlyAdapter(transport=transport).read_krx_vi_evidence(
            symbol="005930", access_token="access-token"
        )

    assert len(transport.requests) == 1


@pytest.mark.parametrize("symbol", ("5930", "0059300", "00A930", "00593\n"))
@pytest.mark.asyncio
async def test_rejects_invalid_symbol_before_transport(symbol: object) -> None:
    transport = ScriptedTransport(responses=[])

    with pytest.raises(ValueError) as raised:
        await TossDomesticViReadOnlyAdapter(transport=transport).read_krx_vi_evidence(
            symbol=cast(str, symbol), access_token="access-token"
        )

    assert str(raised.value) == "Toss KRX VI symbol is invalid"
    assert transport.requests == []


@pytest.mark.parametrize("access_token", ("", "token\nvalue", 1))
@pytest.mark.asyncio
async def test_rejects_invalid_access_token_before_transport(
    access_token: object,
) -> None:
    transport = ScriptedTransport(responses=[])

    with pytest.raises(ValueError) as raised:
        await TossDomesticViReadOnlyAdapter(transport=transport).read_krx_vi_evidence(
            symbol="005930", access_token=cast(str, access_token)
        )

    assert str(raised.value) == "Toss KRX VI access token is invalid"
    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ("submit", "cancel", "replace"))
async def test_write_methods_are_disabled_before_transport(method: str) -> None:
    transport = ScriptedTransport(responses=[])
    adapter = TossDomesticViReadOnlyAdapter(transport=transport)

    with pytest.raises(BrokerWriteDisabled) as raised:
        await getattr(adapter, method)(command=object())

    assert str(raised.value) == "Toss domestic VI write adapter is not enabled"
    assert transport.requests == []


@dataclass(frozen=True, slots=True)
class _PrivacyCapture:
    forbidden: tuple[object, ...]
    private_contents: tuple[str, ...]
    request_count: int


_privacy_capture: _PrivacyCapture | None = None


async def _provider_failure_privacy_probe() -> BaseException:
    global _privacy_capture
    token = "".join(("vi-private-token", "-805"))
    symbol = "".join(("005", "930"))
    raw = bytes(
        bytearray(
            b'{"result":[{"warningType":"VI_STATIC",'
            b'"exchange":"private-warning-806\\n"}]}'
        )
    )
    response = BrokerResponse(status=200, body=raw)
    transport = ScriptedTransport(responses=[response])
    adapter = TossDomesticViReadOnlyAdapter(transport=transport)
    request: BrokerRequest | None = None
    public_error: BaseException | None = None
    try:
        with pytest.raises(TossIncompleteKrxViSnapshot) as raised:
            await adapter.read_krx_vi_evidence(symbol=symbol, access_token=token)
        public_error = raised.value
        request = transport.requests[0]
        assert request is not None
        _privacy_capture = _PrivacyCapture(
            forbidden=(token, symbol, raw, request, response, transport, adapter),
            private_contents=(token, symbol, raw.decode("utf-8")),
            request_count=len(transport.requests),
        )
        assert public_error is not None
        return public_error
    finally:
        del token, symbol, raw, response, transport, adapter, request, public_error


async def _transport_failure_privacy_probe() -> BaseException:
    global _privacy_capture
    token = "".join(("vi-private-token", "-807"))
    symbol = "".join(("005", "930"))
    transport_error = OSError("private-transport-808")
    transport = ScriptedTransport(responses=[transport_error])
    adapter = TossDomesticViReadOnlyAdapter(transport=transport)
    request: BrokerRequest | None = None
    public_error: BaseException | None = None
    try:
        with pytest.raises(TossIncompleteKrxViSnapshot) as raised:
            await adapter.read_krx_vi_evidence(symbol=symbol, access_token=token)
        public_error = raised.value
        request = transport.requests[0]
        assert request is not None
        _privacy_capture = _PrivacyCapture(
            forbidden=(token, symbol, request, transport_error, transport, adapter),
            private_contents=(token, symbol, "private-transport-808"),
            request_count=len(transport.requests),
        )
        assert public_error is not None
        return public_error
    finally:
        del (
            token,
            symbol,
            transport_error,
            transport,
            adapter,
            request,
            public_error,
        )


async def _invalid_token_privacy_probe() -> BaseException:
    global _privacy_capture
    token = "".join(("invalid", "\n", "token"))
    symbol = "".join(("005", "930"))
    transport = ScriptedTransport(responses=[])
    adapter = TossDomesticViReadOnlyAdapter(transport=transport)
    public_error: BaseException | None = None
    try:
        with pytest.raises(ValueError) as raised:
            await adapter.read_krx_vi_evidence(symbol=symbol, access_token=token)
        public_error = raised.value
        _privacy_capture = _PrivacyCapture(
            forbidden=(token, symbol, transport, adapter),
            private_contents=(token, symbol),
            request_count=len(transport.requests),
        )
        assert public_error is not None
        return public_error
    finally:
        del token, symbol, transport, adapter, public_error


async def _invalid_symbol_privacy_probe() -> BaseException:
    global _privacy_capture
    token = "".join(("vi-private-token", "-809"))
    symbol = "".join(("005", "93x"))
    transport = ScriptedTransport(responses=[])
    adapter = TossDomesticViReadOnlyAdapter(transport=transport)
    public_error: BaseException | None = None
    try:
        with pytest.raises(ValueError) as raised:
            await adapter.read_krx_vi_evidence(symbol=symbol, access_token=token)
        public_error = raised.value
        _privacy_capture = _PrivacyCapture(
            forbidden=(token, symbol, transport, adapter),
            private_contents=(token, symbol),
            request_count=len(transport.requests),
        )
        assert public_error is not None
        return public_error
    finally:
        del token, symbol, transport, adapter, public_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("factory", "expected_exception", "expected_request_count"),
    (
        (_provider_failure_privacy_probe, TossIncompleteKrxViSnapshot, 1),
        (_transport_failure_privacy_probe, TossIncompleteKrxViSnapshot, 1),
        (_invalid_token_privacy_probe, ValueError, 0),
        (_invalid_symbol_privacy_probe, ValueError, 0),
    ),
)
async def test_public_failures_do_not_retain_sensitive_object_identities(
    factory: Callable[[], Awaitable[BaseException]],
    expected_exception: type[BaseException],
    expected_request_count: int,
) -> None:
    raised = await factory()
    capture = _privacy_capture
    assert capture is not None

    assert isinstance(raised, expected_exception)
    assert capture.request_count == expected_request_count
    assert raised.__cause__ is None
    assert raised.__context__ is None
    reachable = tuple(_error_reachable_values(raised))
    assert any(isinstance(value, FrameType) for value in reachable)
    assert all(
        all(value is not forbidden for value in reachable)
        for forbidden in capture.forbidden
    )
    assert all(
        not _contains_private_content(value, capture.private_contents)
        for value in reachable
    )


class _CopiedPrivateContent:
    def __init__(self, content: str) -> None:
        self._content = content

    def __repr__(self) -> str:
        return f"copied-private-content={self._content}"


def test_private_content_detector_catches_copies_in_text_bytes_and_repr() -> None:
    content = "private-copy-sentinel-810"

    assert _contains_private_content(f"Bearer {content}", (content,))
    assert _contains_private_content(content.encode("utf-8"), (content,))
    assert _contains_private_content(_CopiedPrivateContent(content), (content,))
    assert not _contains_private_content("unrelated", (content,))


def _error_reachable_values(error: BaseException) -> Iterator[object]:
    pending: list[object] = [error]
    visited: set[int] = set()
    while pending and len(visited) < 750:
        value = pending.pop()
        if id(value) in visited:
            continue
        visited.add(id(value))
        yield value
        if isinstance(value, BaseException):
            pending.extend(value.args)
            if value.__cause__ is not None:
                pending.append(value.__cause__)
            if value.__context__ is not None:
                pending.append(value.__context__)
            pending.append(value.__traceback__)
        elif isinstance(value, TracebackType):
            pending.extend((value.tb_frame, value.tb_next))
        elif isinstance(value, FrameType):
            pending.extend(value.f_locals.values())
            caller = value.f_back
            for _ in range(6):
                if caller is None:
                    break
                pending.append(caller)
                caller = caller.f_back
        elif isinstance(value, (tuple, list, set, frozenset)):
            pending.extend(cast(tuple[object, ...], value))
        elif isinstance(value, dict):
            pending.extend(cast(dict[object, object], value).items())
        elif isinstance(value, FunctionType):
            if value.__closure__ is not None:
                pending.extend(cell.cell_contents for cell in value.__closure__)
            pending.extend(value.__defaults__ or ())
            if value.__kwdefaults__ is not None:
                pending.extend(value.__kwdefaults__.values())
        elif isinstance(value, MethodType):
            pending.extend((value.__self__, value.__func__))
        elif hasattr(value, "__dict__"):
            pending.extend(cast(dict[str, object], value.__dict__).values())
        else:
            for owner in type(value).__mro__:
                raw_slots = owner.__dict__.get("__slots__")
                slots: tuple[str, ...]
                if isinstance(raw_slots, str):
                    slots = (raw_slots,)
                elif isinstance(raw_slots, tuple):
                    candidate_slots = cast(tuple[object, ...], raw_slots)
                    if all(isinstance(candidate, str) for candidate in candidate_slots):
                        slots = tuple(
                            cast(str, candidate) for candidate in candidate_slots
                        )
                    else:
                        slots = ()
                else:
                    slots = ()
                for slot in slots:
                    if hasattr(value, slot):
                        pending.append(getattr(value, slot))


def _contains_private_content(value: object, contents: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(content in value for content in contents)
    if isinstance(value, bytes):
        return any(content.encode("utf-8") in value for content in contents)
    try:
        rendered = _bounded_repr(value)
    except Exception:
        return False
    return any(content in rendered for content in contents)


def _bounded_repr(value: object) -> str:
    renderer = reprlib.Repr()
    renderer.maxother = 1_024
    renderer.maxstring = 1_024
    return renderer.repr(value)


def test_module_has_only_shared_broker_imports_and_clean_fresh_import() -> None:
    module_path = Path("src/autotrader/integrations/brokers/toss/domestic_vi.py")
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    forbidden = (
        "adapter",
        "execution",
        "apps",
        "runtime",
        "persistence",
        "config",
        "risk",
        "observability",
        "contracts",
    )
    assert all(
        not isinstance(node, ast.Import)
        or all(not alias.name.startswith("autotrader.") for alias in node.names)
        for node in ast.walk(tree)
    )
    assert all(
        not isinstance(node, ast.ImportFrom)
        or node.module is None
        or not node.module.startswith("autotrader.")
        or node.module == "autotrader.integrations.brokers.common"
        for node in ast.walk(tree)
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import autotrader.integrations.brokers.toss.domestic_vi; "
            "print('\\n'.join(sorted(sys.modules)))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = set(completed.stdout.splitlines())
    assert not any(
        module.startswith("autotrader.") and any(part in module for part in forbidden)
        for module in loaded
    )


def test_warning_value_objects_reject_mutable_or_invalid_values() -> None:
    with pytest.raises(ValueError):
        TossKrxViWarning("VI_STATIC", "KRX", date(2026, 8, 2), date(2026, 8, 1))
    assert (
        TossKrxViWarning("VI_STATIC", None, None, date(2026, 8, 1)).start_date is None
    )
    with pytest.raises(ValueError):
        TossKrxViEvidence(warnings=cast(tuple[TossKrxViWarning, ...], []))
