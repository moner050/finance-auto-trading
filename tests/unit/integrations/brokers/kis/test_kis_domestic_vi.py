from __future__ import annotations

import sys
import traceback
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from subprocess import run
from types import FunctionType
from typing import cast

import pytest

from autotrader.integrations.brokers.common import (
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)
from autotrader.integrations.brokers.kis.adapter import KisReadCredentials
from autotrader.integrations.brokers.kis.domestic_vi import (
    KisIncompleteDomesticViSnapshot,
    KisViMarket,
)


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
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        raise RuntimeError(self.raw_body.decode())


def credentials() -> KisReadCredentials:
    return KisReadCredentials(access_token="token", app_key="app", app_secret="secret")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market", "market_code"),
    [("KOSPI", "K"), ("KOSDAQ", "Q")],
)
async def test_vi_snapshot_uses_exact_documented_read_contract(
    market: str, market_code: str
) -> None:
    from autotrader.integrations.brokers.kis.domestic_vi import (
        KisDomesticViReadOnlyAdapter,
    )

    transport = RecordingTransport(
        response=BrokerResponse(
            status=200, body=b'{"rt_cd":"0","output":[{"z":"2","a":"1"}]}'
        )
    )

    snapshot = await KisDomesticViReadOnlyAdapter(
        transport=transport
    ).read_complete_snapshot(
        credentials=credentials(),
        market=KisViMarket(market),
        symbol="005930",
        business_date=date(2026, 8, 11),
        max_pages=1,
    )

    assert snapshot.pages[0].records == ((("a", "1"), ("z", "2")),)
    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path=(
                "/uapi/domestic-stock/v1/quotations/inquire-vi-status?"
                "FID_DIV_CLS_CODE=0&FID_COND_SCR_DIV_CODE=20139&"
                f"FID_MRKT_CLS_CODE={market_code}&FID_INPUT_ISCD=005930&"
                "FID_RANK_SORT_CLS_CODE=0&FID_INPUT_DATE_1=20260811&"
                "FID_TRGT_CLS_CODE=&FID_TRGT_EXLS_CLS_CODE="
            ),
            headers=(
                ("authorization", "Bearer token"),
                ("appkey", "app"),
                ("appsecret", "secret"),
                ("tr_id", "FHPST01390000"),
                ("custtype", "P"),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_vi_snapshot_follows_only_m_continuation() -> None:
    from autotrader.integrations.brokers.kis.domestic_vi import (
        KisDomesticViReadOnlyAdapter,
        KisViMarket,
    )

    transport = ScriptedTransport(
        responses=[
            BrokerResponse(
                status=200,
                body=b'{"rt_cd":"0","output":[{"page":"1"}]}',
                headers=(("tr_cont", "M"),),
            ),
            BrokerResponse(status=200, body=b'{"rt_cd":"0","output":[{"page":"2"}]}'),
        ]
    )
    snapshot = await KisDomesticViReadOnlyAdapter(
        transport=transport
    ).read_complete_snapshot(
        credentials=credentials(),
        market=KisViMarket.KOSPI,
        symbol="005930",
        business_date=date(2026, 8, 11),
        max_pages=2,
    )

    assert tuple(page.records for page in snapshot.pages) == (
        ((("page", "1"),),),
        ((("page", "2"),),),
    )
    assert transport.requests[1].headers == (
        ("appkey", "app"),
        ("appsecret", "secret"),
        ("authorization", "Bearer token"),
        ("custtype", "P"),
        ("tr_cont", "N"),
        ("tr_id", "FHPST01390000"),
    )
    assert transport.requests[1].path == transport.requests[0].path


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", ["F", "X"])
async def test_vi_snapshot_fails_closed_for_unsupported_continuation(
    continuation: str,
) -> None:
    from autotrader.integrations.brokers.kis.domestic_vi import (
        KisDomesticViReadOnlyAdapter,
        KisIncompleteDomesticViSnapshot,
        KisViMarket,
    )

    transport = RecordingTransport(
        response=BrokerResponse(
            status=200,
            body=b'{"rt_cd":"0","output":[{"page":"1"}]}',
            headers=(("tr_cont", continuation),),
        )
    )
    with pytest.raises(
        KisIncompleteDomesticViSnapshot,
        match=r"^KIS domestic VI snapshot is incomplete$",
    ):
        await KisDomesticViReadOnlyAdapter(transport=transport).read_complete_snapshot(
            credentials=credentials(),
            market=KisViMarket.KOSPI,
            symbol="005930",
            business_date=date(2026, 8, 11),
            max_pages=2,
        )
    assert len(transport.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        BrokerResponse(status=500, body=b"raw-provider"),
        BrokerResponse(status=200, body=b"invalid-json"),
        BrokerResponse(status=200, body=b'{"rt_cd":"1","output":[]}'),
        BrokerResponse(status=200, body=b'{"rt_cd":"0"}'),
        BrokerResponse(status=200, body=b'{"rt_cd":"0","output":[{"a":""}]}'),
        BrokerResponse(status=200, body=b'{"rt_cd":"0","output":[{"a":1}]}'),
    ],
)
async def test_vi_snapshot_fails_closed_for_malformed_provider_evidence(
    response: BrokerResponse,
) -> None:
    from autotrader.integrations.brokers.kis.domestic_vi import (
        KisDomesticViReadOnlyAdapter,
        KisIncompleteDomesticViSnapshot,
        KisViMarket,
    )

    with pytest.raises(
        KisIncompleteDomesticViSnapshot,
        match=r"^KIS domestic VI snapshot is incomplete$",
    ):
        await KisDomesticViReadOnlyAdapter(
            transport=RecordingTransport(response=response)
        ).read_complete_snapshot(
            credentials=credentials(),
            market=KisViMarket.KOSPI,
            symbol="005930",
            business_date=date(2026, 8, 11),
            max_pages=1,
        )


@pytest.mark.asyncio
async def test_vi_snapshot_rejects_repeated_page_and_page_exhaustion() -> None:
    from autotrader.integrations.brokers.kis.domestic_vi import (
        KisDomesticViReadOnlyAdapter,
        KisIncompleteDomesticViSnapshot,
        KisViMarket,
    )

    repeated = b'{"rt_cd":"0","output":[{"page":"same"}]}'
    for responses, max_pages in (
        (
            [
                BrokerResponse(status=200, body=repeated, headers=(("tr_cont", "M"),)),
                BrokerResponse(status=200, body=repeated),
            ],
            2,
        ),
        ([BrokerResponse(status=200, body=repeated, headers=(("tr_cont", "M"),))], 1),
    ):
        with pytest.raises(KisIncompleteDomesticViSnapshot):
            await KisDomesticViReadOnlyAdapter(
                transport=ScriptedTransport(responses=responses)
            ).read_complete_snapshot(
                credentials=credentials(),
                market=KisViMarket.KOSPI,
                symbol="005930",
                business_date=date(2026, 8, 11),
                max_pages=max_pages,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market", "symbol", "business_date", "max_pages", "message"),
    [
        (object(), "005930", date(2026, 8, 11), 1, "KIS domestic VI market is invalid"),
        (
            KisViMarket.KOSPI,
            "00593A",
            date(2026, 8, 11),
            1,
            "KIS domestic VI symbol is invalid",
        ),
        (
            KisViMarket.KOSPI,
            "005930",
            datetime(2026, 8, 11),
            1,
            "KIS domestic VI date is invalid",
        ),
        (
            KisViMarket.KOSPI,
            "005930",
            date(2026, 8, 11),
            True,
            "KIS domestic VI page limit is invalid",
        ),
        (
            KisViMarket.KOSPI,
            "005930",
            date(2026, 8, 11),
            11,
            "KIS domestic VI page limit is invalid",
        ),
    ],
)
async def test_vi_snapshot_rejects_invalid_input_before_transport(
    market: object,
    symbol: str,
    business_date: date,
    max_pages: int,
    message: str,
) -> None:
    from autotrader.integrations.brokers.kis.domestic_vi import (
        KisDomesticViReadOnlyAdapter,
    )

    transport = RecordingTransport(
        response=BrokerResponse(status=200, body=b'{"rt_cd":"0","output":[]}')
    )
    with pytest.raises(ValueError, match=f"^{message}$"):
        await KisDomesticViReadOnlyAdapter(transport=transport).read_complete_snapshot(
            credentials=credentials(),
            market=cast(KisViMarket, market),
            symbol=symbol,
            business_date=business_date,
            max_pages=max_pages,
        )
    assert transport.requests == []


def test_vi_evidence_rejects_mutable_or_non_text_records() -> None:
    from autotrader.integrations.brokers.kis.domestic_vi import (
        KisDomesticViEvidencePage,
    )

    with pytest.raises(ValueError):
        KisDomesticViEvidencePage(
            records=cast(tuple[tuple[tuple[str, str], ...], ...], [])
        )
    with pytest.raises(ValueError):
        KisDomesticViEvidencePage(
            records=cast(
                tuple[tuple[tuple[str, str], ...], ...],
                ((("a", "1"),), ("not-a-record",)),
            )
        )
    with pytest.raises(ValueError):
        KisDomesticViEvidencePage(records=((("a", "line\nbreak"),),))


def test_vi_reader_fresh_import_has_no_operational_dependencies() -> None:
    code = """
import sys
import autotrader.integrations.brokers.kis.domestic_vi

prefixes = (
    "autotrader.execution", "autotrader.apps", "autotrader.operations",
    "autotrader.persistence", "autotrader.runtime", "autotrader.config",
    "autotrader.risk", "autotrader.observability", "autotrader.contracts",
)
loaded = [
    name for name in sys.modules
    if name in prefixes or any(name.startswith(prefix + ".") for prefix in prefixes)
]
raise SystemExit(1 if loaded else 0)
"""
    completed = run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["submit", "cancel", "replace"])
async def test_vi_adapter_disables_all_writes_before_transport(method: str) -> None:
    from autotrader.integrations.brokers.kis.domestic_vi import (
        KisDomesticViReadOnlyAdapter,
    )

    transport = RecordingTransport(
        response=BrokerResponse(status=200, body=b'{"rt_cd":"0","output":[]}')
    )
    with pytest.raises(
        BrokerWriteDisabled,
        match=r"^KIS domestic VI write adapter is not enabled$",
    ):
        await getattr(KisDomesticViReadOnlyAdapter(transport=transport), method)(
            command={"secret": "not-used"}
        )
    assert transport.requests == []


_SENSITIVE_TOKEN = "SENTINEL_VI_TOKEN"
_SENSITIVE_APP_KEY = "SENTINEL_VI_APP_KEY"
_SENSITIVE_APP_SECRET = "SENTINEL_VI_APP_SECRET"
_SENSITIVE_RAW = b"SENTINEL_VI_RAW"
_SENSITIVE_SYMBOL = "005930"
_SENSITIVE_DATE = date(2026, 8, 11)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["raw", "transport"])
async def test_vi_public_errors_do_not_retain_sensitive_traceback_values(
    scenario: str,
) -> None:
    from autotrader.integrations.brokers.kis.domestic_vi import (
        KisIncompleteDomesticViSnapshot,
    )

    error, request_count, forbidden_ids = await _sensitive_incomplete_snapshot_error(
        scenario
    )

    assert type(error) is KisIncompleteDomesticViSnapshot
    assert request_count == 1
    assert not _traceback_reaches_forbidden_values(error, forbidden_ids)
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.asyncio
async def test_vi_invalid_input_error_does_not_retain_sensitive_traceback_values() -> (
    None
):
    error, request_count, forbidden_ids = await _sensitive_invalid_input_error()

    assert type(error) is ValueError
    assert request_count == 0
    assert not _traceback_reaches_forbidden_values(error, forbidden_ids)
    assert error.__cause__ is None
    assert error.__context__ is None


async def _sensitive_incomplete_snapshot_error(
    scenario: str,
) -> tuple[KisIncompleteDomesticViSnapshot, int, frozenset[int]]:
    from autotrader.integrations.brokers.kis.domestic_vi import (
        KisDomesticViReadOnlyAdapter,
        KisViMarket,
    )

    credentials_value = KisReadCredentials(
        access_token=_SENSITIVE_TOKEN,
        app_key=_SENSITIVE_APP_KEY,
        app_secret=_SENSITIVE_APP_SECRET,
    )
    response: BrokerResponse | None = None
    if scenario == "raw":
        response = BrokerResponse(status=200, body=_SENSITIVE_RAW)
        transport: RecordingTransport | RaisingTransport = RecordingTransport(
            response=response
        )
    elif scenario == "transport":
        transport = RaisingTransport(raw_body=_SENSITIVE_RAW)
    else:
        raise AssertionError(f"unexpected sensitive scenario: {scenario}")
    adapter = KisDomesticViReadOnlyAdapter(transport=transport)
    try:
        await adapter.read_complete_snapshot(
            credentials=credentials_value,
            market=KisViMarket.KOSPI,
            symbol=_SENSITIVE_SYMBOL,
            business_date=_SENSITIVE_DATE,
            max_pages=1,
        )
    except KisIncompleteDomesticViSnapshot as error:
        request_count = len(transport.requests)
        forbidden_ids = frozenset(
            id(value)
            for value in (
                _SENSITIVE_TOKEN,
                _SENSITIVE_APP_KEY,
                _SENSITIVE_APP_SECRET,
                _SENSITIVE_RAW,
                _SENSITIVE_SYMBOL,
                _SENSITIVE_DATE,
                credentials_value,
                transport,
                response,
                *transport.requests,
            )
            if value is not None
        )
        del credentials_value, transport, response, adapter
        return error, request_count, forbidden_ids
    raise AssertionError("expected incomplete VI snapshot")


async def _sensitive_invalid_input_error() -> tuple[ValueError, int, frozenset[int]]:
    from autotrader.integrations.brokers.kis.domestic_vi import (
        KisDomesticViReadOnlyAdapter,
    )

    credentials_value = KisReadCredentials(
        access_token=_SENSITIVE_TOKEN,
        app_key=_SENSITIVE_APP_KEY,
        app_secret=_SENSITIVE_APP_SECRET,
    )
    response = BrokerResponse(status=200, body=_SENSITIVE_RAW)
    transport = RecordingTransport(response=response)
    adapter = KisDomesticViReadOnlyAdapter(transport=transport)
    invalid_market = object()
    try:
        await adapter.read_complete_snapshot(
            credentials=credentials_value,
            market=cast(KisViMarket, invalid_market),
            symbol=_SENSITIVE_SYMBOL,
            business_date=_SENSITIVE_DATE,
            max_pages=1,
        )
    except ValueError as error:
        request_count = len(transport.requests)
        forbidden_ids = frozenset(
            id(value)
            for value in (
                _SENSITIVE_TOKEN,
                _SENSITIVE_APP_KEY,
                _SENSITIVE_APP_SECRET,
                _SENSITIVE_RAW,
                _SENSITIVE_SYMBOL,
                _SENSITIVE_DATE,
                credentials_value,
                transport,
                response,
                *transport.requests,
            )
        )
        del credentials_value, transport, response, adapter, invalid_market
        return error, request_count, forbidden_ids
    raise AssertionError("expected invalid VI input")


def _traceback_reaches_forbidden_values(
    error: BaseException, forbidden_ids: frozenset[int]
) -> bool:
    frames = [frame for frame, _ in traceback.walk_tb(error.__traceback__)]
    seen_frames: set[int] = set()
    while frames:
        frame = frames.pop()
        frame_id = id(frame)
        if frame_id in seen_frames:
            continue
        seen_frames.add(frame_id)
        values = tuple(frame.f_locals.values())
        if any(id(value) in forbidden_ids for value in values):
            return True
        if any(
            _object_graph_reaches_forbidden_value(value, forbidden_ids)
            for value in values
        ):
            return True
        if frame.f_back is not None:
            frames.append(frame.f_back)
    return False


def _object_graph_reaches_forbidden_value(
    root: object, forbidden_ids: frozenset[int]
) -> bool:
    pending: list[tuple[object, int]] = [(root, 0)]
    seen: set[int] = set()
    visited = 0
    while pending and visited < 256:
        value, depth = pending.pop()
        visited += 1
        object_value = value
        if id(object_value) in forbidden_ids:
            return True
        identity = id(object_value)
        if identity in seen or depth >= 6:
            continue
        seen.add(identity)
        next_depth = depth + 1
        if isinstance(object_value, Mapping):
            mapping = cast(Mapping[object, object], object_value)
            for key, nested_value in mapping.items():
                pending.append((key, next_depth))
                pending.append((nested_value, next_depth))
        elif isinstance(object_value, list):
            pending.extend(
                (item, next_depth) for item in cast(list[object], object_value)
            )
        elif isinstance(object_value, tuple):
            pending.extend(
                (item, next_depth) for item in cast(tuple[object, ...], object_value)
            )
        elif isinstance(object_value, (set, frozenset)):
            pending.extend(
                (item, next_depth)
                for item in cast(set[object] | frozenset[object], object_value)
            )
        if isinstance(object_value, FunctionType):
            pending.extend(
                (item, next_depth) for item in object_value.__defaults__ or ()
            )
            pending.extend(
                (item, next_depth)
                for item in (object_value.__kwdefaults__ or {}).values()
            )
            for cell in object_value.__closure__ or ():
                with suppress(ValueError):
                    pending.append((cell.cell_contents, next_depth))
        dataclass_value = _plain_object(cast(object, object_value))
        if is_dataclass(dataclass_value) and not isinstance(dataclass_value, type):
            pending.extend(
                (getattr(dataclass_value, item.name), next_depth)
                for item in fields(dataclass_value)
            )
        attribute_value = _plain_object(cast(object, object_value))
        attributes = cast(object, getattr(attribute_value, "__dict__", None))
        if isinstance(attributes, Mapping):
            attribute_mapping = cast(Mapping[object, object], attributes)
            pending.extend((item, next_depth) for item in attribute_mapping.values())
    return False


def _plain_object(value: object) -> object:
    return value
