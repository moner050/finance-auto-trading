from __future__ import annotations

import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from types import FunctionType
from typing import cast

import pytest

from autotrader.integrations.brokers.common import (
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)
from autotrader.integrations.brokers.kis import domestic_index
from autotrader.integrations.brokers.kis.adapter import KisReadCredentials
from autotrader.integrations.brokers.kis.domestic_index import (
    KisDomesticIndexEvidencePage,
    KisDomesticIndexEvidenceSnapshot,
    KisDomesticIndexReadOnlyAdapter,
    KisIncompleteDomesticIndexSnapshot,
    KisProviderEvidenceRecord,
)

_SENSITIVE_SENTINELS = (
    "SENTINEL_INDEX_TOKEN",
    "SENTINEL_INDEX_APP_KEY",
    "SENTINEL_INDEX_APP_SECRET",
    "SENTINEL_INDEX_RAW_PROVIDER",
)
_SENSITIVE_GRAPH_DEPTH_LIMIT = 6
_SENSITIVE_GRAPH_NODE_LIMIT = 256


@dataclass
class RecordingTransport:
    response: BrokerResponse
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return self.response


@dataclass
class ScriptedTransport:
    responses: list[BrokerResponse]
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return self.responses.pop(0)


@dataclass
class RaisingTransport:
    raw_body: bytes
    request_value: BrokerRequest | None = None

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.request_value = request
        raise RuntimeError(self.raw_body.decode())


def credentials() -> KisReadCredentials:
    return KisReadCredentials(access_token="token", app_key="app", app_secret="secret")


@pytest.mark.asyncio
async def test_category_snapshot_preserves_immutable_provider_evidence() -> None:
    transport = RecordingTransport(
        response=BrokerResponse(
            status=200,
            body=(
                b'{"rt_cd":"0","output1":{"bstp_nmix_prpr":"2800"},'
                b'"output2":[{"bstp_nmix_prpr":"3100","prdy_ctrt":"1.2"}]}'
            ),
        )
    )

    snapshot = await KisDomesticIndexReadOnlyAdapter(
        transport=transport
    ).read_complete_category_snapshot(credentials=credentials(), max_pages=1)

    assert snapshot.pages[0].output1 == (("bstp_nmix_prpr", "2800"),)
    assert snapshot.pages[0].output2 == (
        (("bstp_nmix_prpr", "3100"), ("prdy_ctrt", "1.2")),
    )
    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path=(
                "/uapi/domestic-stock/v1/quotations/inquire-index-category-price?"
                "FID_COND_MRKT_DIV_CODE=U&FID_INPUT_ISCD=0001&"
                "FID_COND_SCR_DIV_CODE=20214&FID_MRKT_CLS_CODE=K&"
                "FID_BLNG_CLS_CODE=0"
            ),
            headers=(
                ("authorization", "Bearer token"),
                ("appkey", "app"),
                ("appsecret", "secret"),
                ("tr_id", "FHPUP02140000"),
                ("custtype", "P"),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_category_snapshot_preserves_empty_optional_field_value() -> None:
    transport = RecordingTransport(
        response=BrokerResponse(
            status=200,
            body=(
                b'{"rt_cd":"0","output1":{"page":"1"},'
                b'"output2":[{"optional_field":""}]}'
            ),
        )
    )

    snapshot = await KisDomesticIndexReadOnlyAdapter(
        transport=transport
    ).read_complete_category_snapshot(credentials=credentials(), max_pages=1)

    assert snapshot.pages[0].output2 == ((("optional_field", ""),),)


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", ["M", "F"])
async def test_category_snapshot_follows_only_provider_directed_continuation(
    continuation: str,
) -> None:
    transport = ScriptedTransport(
        responses=[
            BrokerResponse(
                status=200,
                body=b'{"rt_cd":"0","output1":{"page":"1"},"output2":[]}',
                headers=(("tr_cont", continuation),),
            ),
            BrokerResponse(
                status=200,
                body=b'{"rt_cd":"0","output1":{"page":"2"},"output2":[]}',
            ),
        ]
    )

    snapshot = await KisDomesticIndexReadOnlyAdapter(
        transport=transport
    ).read_complete_category_snapshot(credentials=credentials(), max_pages=2)

    assert snapshot.pages == (
        KisDomesticIndexEvidencePage(output1=(("page", "1"),), output2=()),
        KisDomesticIndexEvidencePage(output1=(("page", "2"),), output2=()),
    )
    assert transport.requests[1].path == transport.requests[0].path
    assert ("tr_cont", "N") not in transport.requests[0].headers
    assert ("tr_cont", "N") in transport.requests[1].headers


@pytest.mark.asyncio
async def test_daily_snapshot_uses_exact_documented_read_contract() -> None:
    transport = RecordingTransport(
        response=BrokerResponse(
            status=200,
            body=b'{"rt_cd":"0","output1":{"page":"1"},"output2":[]}',
        )
    )

    snapshot = await KisDomesticIndexReadOnlyAdapter(
        transport=transport
    ).read_complete_daily_snapshot(
        credentials=credentials(),
        index_code="0001",
        start_date=date(2026, 8, 11),
        max_pages=1,
    )

    assert snapshot.pages == (
        KisDomesticIndexEvidencePage(output1=(("page", "1"),), output2=()),
    )
    assert transport.requests[0].path == (
        "/uapi/domestic-stock/v1/quotations/inquire-index-daily-price?"
        "FID_PERIOD_DIV_CODE=D&FID_COND_MRKT_DIV_CODE=U&"
        "FID_INPUT_ISCD=0001&FID_INPUT_DATE_1=20260811"
    )
    assert dict(transport.requests[0].headers)["tr_id"] == "FHPUP02120000"


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", ["M", "F"])
async def test_daily_snapshot_follows_only_provider_directed_continuation(
    continuation: str,
) -> None:
    transport = ScriptedTransport(
        responses=[
            BrokerResponse(
                status=200,
                body=b'{"rt_cd":"0","output1":{"page":"1"},"output2":[]}',
                headers=(("tr_cont", continuation),),
            ),
            BrokerResponse(
                status=200,
                body=b'{"rt_cd":"0","output1":{"page":"2"},"output2":[]}',
                headers=(("tr_cont", "N"),),
            ),
        ]
    )

    snapshot = await KisDomesticIndexReadOnlyAdapter(
        transport=transport
    ).read_complete_daily_snapshot(
        credentials=credentials(),
        index_code="0001",
        start_date=date(2026, 8, 11),
        max_pages=2,
    )

    assert len(snapshot.pages) == 2
    assert transport.requests[1].path == transport.requests[0].path
    assert ("tr_cont", "N") not in transport.requests[0].headers
    assert ("tr_cont", "N") in transport.requests[1].headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("index_code", "start_date", "max_pages"),
    [
        ("001", date(2026, 8, 11), 1),
        ("00001", date(2026, 8, 11), 1),
        ("000A", date(2026, 8, 11), 1),
        ("\uff11\uff12\uff13\uff14", date(2026, 8, 11), 1),
        ("0001", datetime(2026, 8, 11), 1),
        ("0001", date(2026, 8, 11), 0),
        ("0001", date(2026, 8, 11), True),
        ("0001", date(2026, 8, 11), 11),
    ],
)
async def test_daily_snapshot_rejects_invalid_inputs_before_transport(
    index_code: str, start_date: date, max_pages: int
) -> None:
    transport = RecordingTransport(response=BrokerResponse(status=200, body=b"{}"))

    with pytest.raises(ValueError):
        await KisDomesticIndexReadOnlyAdapter(
            transport=transport
        ).read_complete_daily_snapshot(
            credentials=credentials(),
            index_code=index_code,
            start_date=start_date,
            max_pages=max_pages,
        )

    assert transport.requests == []


@pytest.mark.asyncio
async def test_category_snapshot_rejects_continuation_beyond_page_limit() -> None:
    transport = RecordingTransport(
        response=BrokerResponse(
            status=200,
            body=b'{"rt_cd":"0","output1":{"page":"1"},"output2":[]}',
            headers=(("tr_cont", "M"),),
        )
    )

    with pytest.raises(
        KisIncompleteDomesticIndexSnapshot,
        match="KIS domestic index snapshot is incomplete",
    ):
        await KisDomesticIndexReadOnlyAdapter(
            transport=transport
        ).read_complete_category_snapshot(credentials=credentials(), max_pages=1)

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_category_snapshot_rejects_f_continuation_beyond_page_limit() -> None:
    transport = RecordingTransport(
        response=BrokerResponse(
            status=200,
            body=b'{"rt_cd":"0","output1":{"page":"1"},"output2":[]}',
            headers=(("tr_cont", "F"),),
        )
    )

    with pytest.raises(
        KisIncompleteDomesticIndexSnapshot,
        match="KIS domestic index snapshot is incomplete",
    ):
        await KisDomesticIndexReadOnlyAdapter(
            transport=transport
        ).read_complete_category_snapshot(credentials=credentials(), max_pages=1)

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_category_snapshot_rejects_repeated_immutable_page() -> None:
    page = b'{"rt_cd":"0","output1":{"page":"same"},"output2":[]}'
    transport = ScriptedTransport(
        responses=[
            BrokerResponse(
                status=200,
                body=page,
                headers=(("tr_cont", "M"),),
            ),
            BrokerResponse(status=200, body=page),
        ]
    )

    with pytest.raises(
        KisIncompleteDomesticIndexSnapshot,
        match="KIS domestic index snapshot is incomplete",
    ):
        await KisDomesticIndexReadOnlyAdapter(
            transport=transport
        ).read_complete_category_snapshot(credentials=credentials(), max_pages=2)

    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_category_snapshot_rejects_f_repeated_immutable_page() -> None:
    page = b'{"rt_cd":"0","output1":{"page":"same"},"output2":[]}'
    transport = ScriptedTransport(
        responses=[
            BrokerResponse(
                status=200,
                body=page,
                headers=(("tr_cont", "F"),),
            ),
            BrokerResponse(status=200, body=page),
        ]
    )

    with pytest.raises(
        KisIncompleteDomesticIndexSnapshot,
        match="KIS domestic index snapshot is incomplete",
    ):
        await KisDomesticIndexReadOnlyAdapter(
            transport=transport
        ).read_complete_category_snapshot(credentials=credentials(), max_pages=2)

    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_category_snapshot_treats_n_header_as_terminal() -> None:
    transport = RecordingTransport(
        response=BrokerResponse(
            status=200,
            body=b'{"rt_cd":"0","output1":{"page":"1"},"output2":[]}',
            headers=(("tr_cont", "N"),),
        )
    )

    snapshot = await KisDomesticIndexReadOnlyAdapter(
        transport=transport
    ).read_complete_category_snapshot(credentials=credentials(), max_pages=2)

    assert len(snapshot.pages) == 1
    assert len(transport.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("max_pages", [0, True, 11])
async def test_category_snapshot_rejects_invalid_page_limit_before_transport(
    max_pages: int,
) -> None:
    transport = RecordingTransport(response=BrokerResponse(status=200, body=b"{}"))

    with pytest.raises(ValueError):
        await KisDomesticIndexReadOnlyAdapter(
            transport=transport
        ).read_complete_category_snapshot(
            credentials=credentials(), max_pages=max_pages
        )

    assert transport.requests == []


@pytest.mark.parametrize(
    "factory",
    [
        lambda: KisDomesticIndexEvidencePage(
            output1=cast(KisProviderEvidenceRecord, []), output2=()
        ),
        lambda: KisDomesticIndexEvidencePage(
            output1=(), output2=cast(tuple[KisProviderEvidenceRecord, ...], [])
        ),
        lambda: KisDomesticIndexEvidenceSnapshot(
            pages=cast(tuple[KisDomesticIndexEvidencePage, ...], [])
        ),
    ],
)
def test_category_evidence_rejects_mutable_collections(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(ValueError):
        factory()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        BrokerResponse(status=500, body=b"{}"),
        BrokerResponse(status=200, body=b'{"rt_cd":"1","output1":{},"output2":[]}'),
        BrokerResponse(status=200, body=b"not-json"),
        BrokerResponse(status=200, body=b'{"rt_cd":"0","output2":[]}'),
        BrokerResponse(status=200, body=b'{"rt_cd":"0","output1":{},"output2":{}}'),
        BrokerResponse(
            status=200,
            body=b'{"rt_cd":"0","output1":{"field":{"nested":"value"}},"output2":[]}',
        ),
        BrokerResponse(
            status=200,
            body=b'{"rt_cd":"0","output1":{},"output2":[{"field":{"nested":"value"}}]}',
        ),
        BrokerResponse(
            status=200,
            body=b'{"rt_cd":"0","output1":{"field":"line\\nbreak"},"output2":[]}',
        ),
    ],
)
async def test_category_snapshot_fails_closed_for_malformed_provider_evidence(
    response: BrokerResponse,
) -> None:
    with pytest.raises(
        KisIncompleteDomesticIndexSnapshot,
        match="KIS domestic index snapshot is incomplete",
    ):
        await KisDomesticIndexReadOnlyAdapter(
            transport=RecordingTransport(response=response)
        ).read_complete_category_snapshot(credentials=credentials(), max_pages=1)


@pytest.mark.asyncio
async def test_category_snapshot_hides_invalid_json_from_reachable_exceptions() -> None:
    marker = b"invalid-json-provider-marker"

    with pytest.raises(KisIncompleteDomesticIndexSnapshot) as raised:
        await KisDomesticIndexReadOnlyAdapter(
            transport=RecordingTransport(
                response=BrokerResponse(status=200, body=marker)
            )
        ).read_complete_category_snapshot(credentials=credentials(), max_pages=1)

    assert list(_reachable_exceptions(raised.value)) == [raised.value]
    assert marker.decode() not in " ".join(
        f"{error!s} {error.args!r}" for error in _reachable_exceptions(raised.value)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    ["category", "daily", "raw_body", "m_exhaustion", "f_repeat"],
)
async def test_incomplete_snapshot_traceback_does_not_retain_sensitive_values(
    scenario: str,
) -> None:
    error = await _sensitive_incomplete_snapshot_error(scenario)

    assert error.args == ("KIS domestic index snapshot is incomplete",)
    assert error.__cause__ is None and error.__context__ is None
    assert not _traceback_retains_sensitive_values(error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_type", "expected_message"),
    [
        (
            "category_invalid_limit",
            ValueError,
            "KIS domestic index page limit is invalid",
        ),
        ("daily_invalid_index", ValueError, "KIS domestic index code is invalid"),
        ("daily_invalid_date", ValueError, "KIS domestic index start date is invalid"),
        ("daily_invalid_limit", ValueError, "KIS domestic index page limit is invalid"),
        (
            "transport_error",
            KisIncompleteDomesticIndexSnapshot,
            "KIS domestic index snapshot is incomplete",
        ),
    ],
)
async def test_public_error_traceback_does_not_retain_sensitive_values(
    scenario: str,
    expected_type: type[Exception],
    expected_message: str,
) -> None:
    error = await _sensitive_public_error(scenario)

    assert type(error) is expected_type
    assert error.args == (expected_message,)
    assert error.__cause__ is None and error.__context__ is None
    assert not _traceback_retains_sensitive_values(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["submit", "cancel", "replace"])
async def test_write_disabled_traceback_does_not_retain_sensitive_values(
    method: str,
) -> None:
    error, request_count = await _sensitive_write_disabled_error(method)

    assert type(error) is BrokerWriteDisabled
    assert error.args == ("KIS domestic index write adapter is not enabled",)
    assert error.__cause__ is None and error.__context__ is None
    assert request_count == 0
    assert not _traceback_retains_sensitive_values(error)


def test_traceback_sensitive_value_walker_detects_opaque_nested_value() -> None:
    assert _traceback_retains_sensitive_values(_opaque_nested_traceback())


@pytest.mark.asyncio
async def test_category_snapshot_fails_closed_when_output2_is_missing() -> None:
    with pytest.raises(
        KisIncompleteDomesticIndexSnapshot,
        match="KIS domestic index snapshot is incomplete",
    ) as raised:
        await KisDomesticIndexReadOnlyAdapter(
            transport=RecordingTransport(
                response=BrokerResponse(status=200, body=b'{"rt_cd":"0","output1":{}}')
            )
        ).read_complete_category_snapshot(credentials=credentials(), max_pages=1)

    assert list(_reachable_exceptions(raised.value)) == [raised.value]


@pytest.mark.asyncio
async def test_category_snapshot_fails_closed_for_non_string_key_from_decoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        domestic_index.json,
        "loads",
        _non_string_key_payload,
    )

    with pytest.raises(
        KisIncompleteDomesticIndexSnapshot,
        match="KIS domestic index snapshot is incomplete",
    ) as raised:
        await KisDomesticIndexReadOnlyAdapter(
            transport=RecordingTransport(
                response=BrokerResponse(status=200, body=b"{}")
            )
        ).read_complete_category_snapshot(credentials=credentials(), max_pages=1)

    assert list(_reachable_exceptions(raised.value)) == [raised.value]


@pytest.mark.parametrize(
    "record",
    [
        (("", "value"),),
        ((1, "value"),),
        (("field", 1),),
        (("field", {"nested": "value"}),),
        (("field", "line\nbreak"),),
    ],
)
def test_category_evidence_rejects_malformed_field_pairs(record: object) -> None:
    with pytest.raises(ValueError):
        KisDomesticIndexEvidencePage(
            output1=cast(KisProviderEvidenceRecord, record), output2=()
        )


def test_category_evidence_rejects_empty_output1_record() -> None:
    with pytest.raises(ValueError):
        KisDomesticIndexEvidencePage(output1=(), output2=())


@pytest.mark.asyncio
async def test_category_snapshot_fails_closed_for_empty_output1_record() -> None:
    with pytest.raises(
        KisIncompleteDomesticIndexSnapshot,
        match="KIS domestic index snapshot is incomplete",
    ):
        await KisDomesticIndexReadOnlyAdapter(
            transport=RecordingTransport(
                response=BrokerResponse(
                    status=200,
                    body=b'{"rt_cd":"0","output1":{},"output2":[]}',
                )
            )
        ).read_complete_category_snapshot(credentials=credentials(), max_pages=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["submit", "cancel", "replace"])
async def test_category_adapter_disables_all_writes(method: str) -> None:
    adapter = KisDomesticIndexReadOnlyAdapter(
        transport=RecordingTransport(response=BrokerResponse(status=200, body=b"{}"))
    )

    with pytest.raises(
        BrokerWriteDisabled, match="KIS domestic index write adapter is not enabled"
    ):
        await getattr(adapter, method)(command=object())


def _reachable_exceptions(error: BaseException) -> tuple[BaseException, ...]:
    reachable: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        reachable.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return tuple(reachable)


def _non_string_key_payload(body: object) -> object:
    del body
    return {"rt_cd": "0", "output1": {1: "value"}, "output2": []}


def _traceback_retains_sensitive_values(error: BaseException) -> bool:
    return any(
        _object_graph_retains_sensitive_value(local)
        for frame, _ in traceback.walk_tb(error.__traceback__)
        for local in frame.f_locals.values()
    )


def _object_graph_retains_sensitive_value(root: object) -> bool:
    pending: list[tuple[object, int]] = [(root, 0)]
    seen: set[int] = set()
    visited = 0
    while pending and visited < _SENSITIVE_GRAPH_NODE_LIMIT:
        value, depth = pending.pop()
        visited += 1
        object_value = value
        if isinstance(object_value, str):
            if any(sentinel in object_value for sentinel in _SENSITIVE_SENTINELS):
                return True
            continue
        if isinstance(object_value, (bytes, bytearray)):
            if any(
                sentinel.encode() in object_value for sentinel in _SENSITIVE_SENTINELS
            ):
                return True
            continue
        identity = id(object_value)
        if identity in seen:
            continue
        seen.add(identity)
        if depth >= _SENSITIVE_GRAPH_DEPTH_LIMIT:
            continue
        child_depth = depth + 1
        if isinstance(object_value, Mapping):
            mapping = cast(Mapping[object, object], object_value)
            for key, nested_value in mapping.items():
                pending.append((key, child_depth))
                pending.append((nested_value, child_depth))
        elif isinstance(object_value, list):
            pending.extend(
                (item, child_depth) for item in cast(list[object], object_value)
            )
        elif isinstance(object_value, tuple):
            pending.extend(
                (item, child_depth) for item in cast(tuple[object, ...], object_value)
            )
        elif isinstance(object_value, (set, frozenset)):
            pending.extend(
                (item, child_depth)
                for item in cast(set[object] | frozenset[object], object_value)
            )
        if isinstance(object_value, FunctionType):
            pending.extend(
                (item, child_depth) for item in object_value.__defaults__ or ()
            )
            pending.extend(
                (item, child_depth)
                for item in (object_value.__kwdefaults__ or {}).values()
            )
            for cell in object_value.__closure__ or ():
                try:
                    pending.append((cell.cell_contents, child_depth))
                except ValueError:
                    continue
        dataclass_value = _plain_object(value)
        if is_dataclass(dataclass_value) and not isinstance(dataclass_value, type):
            for data_field in fields(dataclass_value):
                pending.append(
                    (
                        cast(object, getattr(dataclass_value, data_field.name)),
                        child_depth,
                    )
                )
        attribute_value = _plain_object(value)
        attributes = cast(object, getattr(attribute_value, "__dict__", None))
        if isinstance(attributes, Mapping):
            attribute_mapping = cast(Mapping[object, object], attributes)
            pending.extend((item, child_depth) for item in attribute_mapping.values())
        slot_value = _plain_object(value)
        for owner in type(slot_value).__mro__:
            slots = cast(object, getattr(owner, "__slots__", ()))
            if isinstance(slots, str):
                slot_names: tuple[object, ...] = (slots,)
            elif isinstance(slots, tuple):
                slot_names = cast(tuple[object, ...], slots)
            else:
                continue
            for slot in slot_names:
                if not isinstance(slot, str) or slot in {"__dict__", "__weakref__"}:
                    continue
                try:
                    pending.append(
                        (cast(object, getattr(slot_value, slot)), child_depth)
                    )
                except AttributeError:
                    continue
    return False


def _plain_object(value: object) -> object:
    return value


class _OpaqueSentinel:
    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return "<opaque sentinel>"


def _opaque_nested_traceback() -> RuntimeError:
    hidden = _opaque_function()
    assert callable(hidden)
    try:
        raise RuntimeError("opaque nested sentinel")
    except RuntimeError as error:
        return error


def _opaque_function() -> Callable[[], object]:
    closure_value = _OpaqueSentinel(_SENSITIVE_SENTINELS[0])

    def hidden(
        default_value: object = _OpaqueSentinel(_SENSITIVE_SENTINELS[1]),
    ) -> object:
        return (closure_value, default_value)

    return hidden


async def _sensitive_incomplete_snapshot_error(
    scenario: str,
) -> KisIncompleteDomesticIndexSnapshot:
    reader = "category"
    max_pages = 1
    if scenario == "category":
        transport: RecordingTransport | ScriptedTransport = RecordingTransport(
            response=BrokerResponse(status=200, body=b'{"rt_cd":"0","output1":{}}')
        )
    elif scenario == "daily":
        reader = "daily"
        transport = RecordingTransport(
            response=BrokerResponse(status=200, body=b'{"rt_cd":"0","output1":{}}')
        )
    elif scenario == "raw_body":
        transport = RecordingTransport(
            response=BrokerResponse(status=200, body=b"SENTINEL_INDEX_RAW_PROVIDER")
        )
    elif scenario == "m_exhaustion":
        transport = RecordingTransport(
            response=BrokerResponse(
                status=200,
                body=_sensitive_evidence_body(),
                headers=(("tr_cont", "M"),),
            )
        )
    elif scenario == "f_repeat":
        max_pages = 2
        transport = ScriptedTransport(
            responses=[
                BrokerResponse(
                    status=200,
                    body=_sensitive_evidence_body(),
                    headers=(("tr_cont", "F"),),
                ),
                BrokerResponse(
                    status=200,
                    body=_sensitive_evidence_body(),
                    headers=(("tr_cont", "N"),),
                ),
            ]
        )
    else:
        raise AssertionError(f"unexpected security scenario: {scenario}")
    adapter = KisDomesticIndexReadOnlyAdapter(transport=transport)
    credentials_value = KisReadCredentials(
        access_token="SENTINEL_INDEX_TOKEN",
        app_key="SENTINEL_INDEX_APP_KEY",
        app_secret="SENTINEL_INDEX_APP_SECRET",
    )
    try:
        if reader == "category":
            await adapter.read_complete_category_snapshot(
                credentials=credentials_value, max_pages=max_pages
            )
        else:
            await adapter.read_complete_daily_snapshot(
                credentials=credentials_value,
                index_code="0001",
                start_date=date(2026, 8, 11),
                max_pages=max_pages,
            )
    except KisIncompleteDomesticIndexSnapshot as error:
        del transport, adapter, credentials_value
        return error
    raise AssertionError("expected an incomplete snapshot")


def _sensitive_evidence_body() -> bytes:
    return (
        b'{"rt_cd":"0","output1":{"marker":"SENTINEL_INDEX_RAW_PROVIDER"},"output2":[]}'
    )


async def _sensitive_public_error(scenario: str) -> Exception:
    credentials_value = KisReadCredentials(
        access_token="SENTINEL_INDEX_TOKEN",
        app_key="SENTINEL_INDEX_APP_KEY",
        app_secret="SENTINEL_INDEX_APP_SECRET",
    )
    if scenario == "transport_error":
        transport: RecordingTransport | RaisingTransport = RaisingTransport(
            raw_body=b"SENTINEL_INDEX_RAW_PROVIDER"
        )
    else:
        transport = RecordingTransport(response=BrokerResponse(status=200, body=b"{}"))
    adapter = KisDomesticIndexReadOnlyAdapter(transport=transport)
    try:
        if scenario == "category_invalid_limit":
            await adapter.read_complete_category_snapshot(
                credentials=credentials_value, max_pages=0
            )
        elif scenario == "daily_invalid_index":
            await adapter.read_complete_daily_snapshot(
                credentials=credentials_value,
                index_code="000A",
                start_date=date(2026, 8, 11),
                max_pages=1,
            )
        elif scenario == "daily_invalid_date":
            await adapter.read_complete_daily_snapshot(
                credentials=credentials_value,
                index_code="0001",
                start_date=datetime(2026, 8, 11),
                max_pages=1,
            )
        elif scenario == "daily_invalid_limit":
            await adapter.read_complete_daily_snapshot(
                credentials=credentials_value,
                index_code="0001",
                start_date=date(2026, 8, 11),
                max_pages=0,
            )
        elif scenario == "transport_error":
            await adapter.read_complete_category_snapshot(
                credentials=credentials_value, max_pages=1
            )
        else:
            raise AssertionError(f"unexpected public error scenario: {scenario}")
    except Exception as error:
        del transport, adapter, credentials_value
        return error
    raise AssertionError("expected a public error")


async def _sensitive_write_disabled_error(
    method: str,
) -> tuple[BrokerWriteDisabled, int]:
    transport = RecordingTransport(
        response=BrokerResponse(status=200, body=b"SENTINEL_INDEX_RAW_PROVIDER")
    )
    adapter = KisDomesticIndexReadOnlyAdapter(transport=transport)
    command = {
        "token": "SENTINEL_INDEX_TOKEN",
        "app_key": "SENTINEL_INDEX_APP_KEY",
        "app_secret": "SENTINEL_INDEX_APP_SECRET",
    }
    try:
        await getattr(adapter, method)(command=command)
    except BrokerWriteDisabled as error:
        request_count = len(transport.requests)
        del transport, adapter, command
        return error, request_count
    raise AssertionError("expected a write-disabled error")
