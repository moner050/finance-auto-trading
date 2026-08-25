from __future__ import annotations

import asyncio
import reprlib
import warnings
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import FrameType, FunctionType, MethodType, TracebackType
from typing import cast
from urllib.request import Request
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

import pytest

from autotrader.domain.krx_instrument_authority import KrxCashMarket
from autotrader.integrations.brokers.common import (
    BrokerRequest,
    BrokerResponse,
    BrokerTransportError,
    BrokerWriteDisabled,
)
from autotrader.integrations.brokers.kis import (
    instrument_master_source as instrument_master_source_module,
)
from autotrader.integrations.brokers.kis.instrument_master_source import (
    KisDownloadedKrxCommonStockAuthority,
    KisIncompleteInstrumentMasterDownload,
    KisInstrumentMasterHttpsTransport,
    download_kis_krx_common_stock_authority,
)

SOURCE_LAST_MODIFIED = "Tue, 18 Aug 2026 09:55:03 GMT"
SOURCE_AT = datetime(2026, 8, 18, 9, 55, 3, tzinfo=UTC)
DOWNLOADED_AT = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)
KOSPI_PATH = "/common/master/kospi_code.mst.zip"
KOSDAQ_PATH = "/common/master/kosdaq_code.mst.zip"
KOSPI_WIDTHS = (
    2,
    1,
    4,
    4,
    4,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    9,
    5,
    5,
    1,
    1,
    1,
    2,
    1,
    1,
    1,
    2,
    2,
    2,
    3,
    1,
    3,
    12,
    12,
    8,
    15,
    21,
    2,
    7,
    1,
    1,
    1,
    1,
    1,
    9,
    9,
    9,
    5,
    9,
    8,
    9,
    3,
    1,
    1,
    1,
)
KOSDAQ_WIDTHS = (
    2,
    1,
    4,
    4,
    4,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    9,
    5,
    5,
    1,
    1,
    1,
    2,
    1,
    1,
    1,
    2,
    2,
    2,
    3,
    1,
    3,
    12,
    12,
    8,
    15,
    21,
    2,
    7,
    1,
    1,
    1,
    1,
    9,
    9,
    9,
    5,
    9,
    8,
    9,
    3,
    1,
    1,
    1,
)


class ScriptedTransport:
    def __init__(self, responses: Sequence[BrokerResponse | BaseException]) -> None:
        self.responses = list(responses)
        self.requests: list[BrokerRequest] = []

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected master request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@dataclass
class FakeHttpResponse:
    status: int = 200
    body: bytes = b"zip"
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    read_amounts: list[int] = field(default_factory=list[int])

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        self.read_amounts.append(amount)
        return self.body if amount < 0 else self.body[:amount]


@dataclass
class RecordingOpener:
    requests: list[Request] = field(default_factory=list[Request])
    response: FakeHttpResponse = field(default_factory=FakeHttpResponse)

    def __call__(self, request: Request, timeout: float) -> FakeHttpResponse:
        del timeout
        self.requests.append(request)
        return self.response


@dataclass(frozen=True, slots=True)
class _PrivacyCapture:
    forbidden: tuple[object, ...]
    private_contents: tuple[str, ...]
    request_count: int


_privacy_capture: _PrivacyCapture | None = None


def _record(
    market: KrxCashMarket,
    symbol: str,
    standard_code: str,
    name: str,
) -> bytes:
    widths = KOSPI_WIDTHS if market is KrxCashMarket.KOSPI else KOSDAQ_WIDTHS
    etp_index = 12 if market is KrxCashMarket.KOSPI else 8
    preferred_index = 54 if market is KrxCashMarket.KOSPI else 49
    fields = [" " * width for width in widths]
    fields[0] = "ST"
    fields[etp_index] = " "
    fields[preferred_index] = "0"
    return (
        symbol.ljust(9) + standard_code.ljust(12) + name + "".join(fields) + "\n"
    ).encode("cp949")


def _valid_kospi_record() -> bytes:
    return _record(KrxCashMarket.KOSPI, "005930", "KR7005930003", "삼성전자")


def _archive(member: str, body: bytes) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(member, body)
    return output.getvalue()


def _archive_entries(entries: Sequence[tuple[str, bytes]]) -> bytes:
    output = BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            for member, body in entries:
                archive.writestr(member, body)
    return output.getvalue()


def _archive_with_info(member: str, body: bytes, info: ZipInfo) -> bytes:
    output = BytesIO()
    info.filename = member
    with ZipFile(output, "w") as archive:
        archive.writestr(info, body)
    return output.getvalue()


def _unsupported_compression_archive(member: str, body: bytes) -> bytes:
    info = ZipInfo(member, date_time=(2026, 8, 18, 9, 55, 2))
    info.compress_type = ZIP_BZIP2
    return _archive_with_info(member, body, info)


def _symlink_archive(member: str, body: bytes) -> bytes:
    info = ZipInfo(member, date_time=(2026, 8, 18, 9, 55, 2))
    info.create_system = 3
    info.external_attr = 0o120777 << 16
    info.compress_type = ZIP_DEFLATED
    return _archive_with_info(member, body, info)


def _directory_mode_archive(member: str, body: bytes) -> bytes:
    info = ZipInfo(member, date_time=(2026, 8, 18, 9, 55, 2))
    info.create_system = 3
    info.external_attr = 0o040755 << 16
    info.compress_type = ZIP_DEFLATED
    return _archive_with_info(member, body, info)


def _encrypted_archive(member: str, body: bytes) -> bytes:
    archive = bytearray(_archive(member, body))
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        header = archive.index(signature)
        flags = int.from_bytes(
            archive[header + flag_offset : header + flag_offset + 2], "little"
        )
        archive[header + flag_offset : header + flag_offset + 2] = (flags | 1).to_bytes(
            2, "little"
        )
    return bytes(archive)


def _crc_corrupt_archive(member: str, body: bytes) -> bytes:
    archive = bytearray(_archive(member, body))
    central_header = archive.index(b"PK\x01\x02")
    archive[central_header + 16] ^= 0x01
    return bytes(archive)


def _response(
    member: str,
    member_body: bytes,
    *,
    status: int = 200,
    headers: tuple[tuple[str, str], ...] | None = None,
    archive_body: bytes | None = None,
) -> BrokerResponse:
    return BrokerResponse(
        status=status,
        body=(_archive(member, member_body) if archive_body is None else archive_body),
        headers=(
            (
                ("Content-Type", "application/zip"),
                ("Last-Modified", SOURCE_LAST_MODIFIED),
            )
            if headers is None
            else headers
        ),
    )


def _responses() -> list[BrokerResponse | BaseException]:
    return [
        _response(
            "kospi_code.mst",
            _record(KrxCashMarket.KOSPI, "005930", "KR7005930003", "삼성전자"),
        ),
        _response(
            "kosdaq_code.mst",
            _record(KrxCashMarket.KOSDAQ, "035720", "KR7035720002", "카카오"),
        ),
    ]


def test_forbidden_zip_fixtures_exercise_exact_metadata_boundaries() -> None:
    with ZipFile(
        BytesIO(_unsupported_compression_archive("kospi_code.mst", b"x"))
    ) as archive:
        assert archive.infolist()[0].compress_type == ZIP_BZIP2
    with ZipFile(BytesIO(_symlink_archive("kospi_code.mst", b"x"))) as archive:
        assert archive.infolist()[0].external_attr >> 16 & 0o170000 == 0o120000
    with ZipFile(BytesIO(_directory_mode_archive("kospi_code.mst", b"x"))) as archive:
        assert archive.infolist()[0].external_attr >> 16 & 0o170000 == 0o040000
        assert not archive.infolist()[0].is_dir()
    with ZipFile(BytesIO(_encrypted_archive("kospi_code.mst", b"x"))) as archive:
        assert archive.infolist()[0].flag_bits & 1
    with (
        ZipFile(BytesIO(_crc_corrupt_archive("kospi_code.mst", b"x"))) as archive,
        pytest.raises(BadZipFile, match="CRC"),
    ):
        archive.read("kospi_code.mst")
    with ZipFile(
        BytesIO(_archive_entries((("kospi_code.mst", b"x"), ("kospi_code.mst", b"x"))))
    ) as archive:
        assert [item.filename for item in archive.infolist()] == [
            "kospi_code.mst",
            "kospi_code.mst",
        ]


@pytest.mark.asyncio
async def test_downloads_exact_two_archives_and_binds_source_times() -> None:
    transport = ScriptedTransport(_responses())

    result = await download_kis_krx_common_stock_authority(
        transport=transport,
        clock=lambda: DOWNLOADED_AT,
    )

    assert transport.requests == [
        BrokerRequest(method="GET", path=KOSPI_PATH),
        BrokerRequest(method="GET", path=KOSDAQ_PATH),
    ]
    assert result.source_last_modified_at == SOURCE_AT
    assert result.downloaded_at is DOWNLOADED_AT
    assert result.snapshot.captured_at == SOURCE_AT
    assert tuple(fact.symbol for fact in result.snapshot.instruments) == (
        "005930",
        "035720",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses",
    [
        [_response("kospi_code.mst", b"x", status=500), _responses()[1]],
        [
            _response(
                "kospi_code.mst",
                b"x",
                headers=(
                    ("Content-Type", "text/plain"),
                    ("Last-Modified", SOURCE_LAST_MODIFIED),
                ),
            ),
            _responses()[1],
        ],
        [BrokerResponse(status=200, body=b"not-zip"), _responses()[1]],
        [_response("wrong.mst", b"x"), _responses()[1]],
        [
            _responses()[0],
            _response(
                "kosdaq_code.mst",
                _record(
                    KrxCashMarket.KOSDAQ,
                    "035720",
                    "KR7035720002",
                    "카카오",
                ),
                headers=(
                    ("Content-Type", "application/zip"),
                    ("Last-Modified", "Tue, 18 Aug 2026 09:55:04 GMT"),
                ),
            ),
        ],
    ],
    ids=("http", "content-type", "corrupt", "member", "cross-update"),
)
async def test_download_failures_are_stable(
    responses: Sequence[BrokerResponse | BaseException],
) -> None:
    transport = ScriptedTransport(responses)

    with pytest.raises(
        KisIncompleteInstrumentMasterDownload,
        match="incomplete",
    ) as raised:
        await download_kis_krx_common_stock_authority(
            transport=transport,
            clock=lambda: DOWNLOADED_AT,
        )

    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kospi_response",
    [
        BrokerResponse(
            status=200,
            body=b"x" * (4 * 1024 * 1024 + 1),
            headers=(
                ("Content-Type", "application/zip"),
                ("Last-Modified", SOURCE_LAST_MODIFIED),
            ),
        ),
        _response(
            "kospi_code.mst",
            b"x" * (8 * 1024 * 1024 + 1),
        ),
        _response(
            "kospi_code.mst",
            _valid_kospi_record(),
            archive_body=_archive_entries(
                (
                    ("kospi_code.mst", _valid_kospi_record()),
                    ("extra.mst", b"x"),
                )
            ),
        ),
        _response(
            "kospi_code.mst",
            b"x",
            headers=(
                ("Content-Type", "application/zip"),
                ("Last-Modified", SOURCE_LAST_MODIFIED),
                ("Last-Modified", SOURCE_LAST_MODIFIED),
            ),
        ),
        _response(
            "kospi_code.mst",
            _valid_kospi_record(),
            archive_body=_unsupported_compression_archive(
                "kospi_code.mst", _valid_kospi_record()
            ),
        ),
        _response(
            "kospi_code.mst",
            _valid_kospi_record(),
            archive_body=_symlink_archive("kospi_code.mst", _valid_kospi_record()),
        ),
        _response(
            "kospi_code.mst",
            _valid_kospi_record(),
            archive_body=_directory_mode_archive(
                "kospi_code.mst", _valid_kospi_record()
            ),
        ),
        _response(
            "kospi_code.mst",
            _valid_kospi_record(),
            archive_body=_encrypted_archive("kospi_code.mst", _valid_kospi_record()),
        ),
        _response(
            "kospi_code.mst",
            _valid_kospi_record(),
            archive_body=_crc_corrupt_archive("kospi_code.mst", _valid_kospi_record()),
        ),
        _response(
            "kospi_code.mst",
            _valid_kospi_record(),
            archive_body=_archive_entries(
                (
                    ("kospi_code.mst", _valid_kospi_record()),
                    ("kospi_code.mst", _valid_kospi_record()),
                )
            ),
        ),
    ],
    ids=(
        "archive-cap",
        "member-cap",
        "extra-member",
        "duplicate-source-time",
        "unsupported-compression",
        "symlink-member",
        "directory-mode-member",
        "encrypted-member",
        "crc-corrupt-member",
        "duplicate-member",
    ),
)
async def test_archive_resource_and_header_boundaries_fail_closed(
    kospi_response: BrokerResponse,
) -> None:
    transport = ScriptedTransport((kospi_response, _responses()[1]))

    with pytest.raises(KisIncompleteInstrumentMasterDownload, match="incomplete"):
        await download_kis_krx_common_stock_authority(
            transport=transport,
            clock=lambda: DOWNLOADED_AT,
        )


def test_archive_cap_is_checked_before_zip_or_parser_validation() -> None:
    response = BrokerResponse(
        status=200,
        body=b"x" * (4 * 1024 * 1024 + 1),
        headers=(
            ("Content-Type", "application/zip"),
            ("Last-Modified", SOURCE_LAST_MODIFIED),
        ),
    )

    validated_member = cast(
        Callable[..., tuple[bytes, datetime]],
        vars(instrument_master_source_module)["_validated_member"],
    )
    with pytest.raises(ValueError, match="archive size"):
        validated_member(
            response=response,
            expected_member="kospi_code.mst",
        )


def test_member_cap_is_checked_before_returning_decompressed_bytes() -> None:
    oversized_archive = _archive(
        "kospi_code.mst",
        b"x" * (8 * 1024 * 1024 + 1),
    )

    read_exact_zip_member = cast(
        Callable[..., bytes],
        vars(instrument_master_source_module)["_read_exact_zip_member"],
    )
    with pytest.raises(ValueError, match="member"):
        read_exact_zip_member(
            oversized_archive,
            expected_member="kospi_code.mst",
        )


@pytest.mark.asyncio
async def test_future_source_or_invalid_local_clock_fails_closed() -> None:
    for clock_value in (
        datetime(2026, 8, 18, 9, 55, 2, tzinfo=UTC),
        datetime(2026, 8, 18, 10, 0),
        datetime(2026, 8, 18, 10, 0, 0, 1, tzinfo=UTC),
    ):
        with pytest.raises(KisIncompleteInstrumentMasterDownload, match="incomplete"):
            await download_kis_krx_common_stock_authority(
                transport=ScriptedTransport(_responses()),
                clock=lambda value=clock_value: value,
            )


@pytest.mark.asyncio
async def test_public_download_value_revalidates_snapshot_and_time_binding() -> None:
    result = await download_kis_krx_common_stock_authority(
        transport=ScriptedTransport(_responses()),
        clock=lambda: DOWNLOADED_AT,
    )

    with pytest.raises(ValueError, match="bind"):
        KisDownloadedKrxCommonStockAuthority(
            snapshot=result.snapshot,
            source_last_modified_at=datetime(2026, 8, 18, 9, 55, 4, tzinfo=UTC),
            downloaded_at=result.downloaded_at,
        )

    object.__setattr__(result.snapshot.instruments[0], "symbol", "BAD")
    with pytest.raises(ValueError, match="snapshot"):
        KisDownloadedKrxCommonStockAuthority(
            snapshot=result.snapshot,
            source_last_modified_at=result.source_last_modified_at,
            downloaded_at=result.downloaded_at,
        )


async def _ordinary_failure_privacy_probe() -> BaseException:
    global _privacy_capture
    provider_error = OSError("private-master-transport-991")
    transport = ScriptedTransport((provider_error,))
    request: BrokerRequest | None = None
    public_error: BaseException | None = None
    try:
        with pytest.raises(KisIncompleteInstrumentMasterDownload) as raised:
            await download_kis_krx_common_stock_authority(
                transport=transport,
                clock=lambda: DOWNLOADED_AT,
            )
        public_error = raised.value
        request = transport.requests[0]
        _privacy_capture = _PrivacyCapture(
            forbidden=(provider_error, request, transport),
            private_contents=("private-master-transport-991",),
            request_count=len(transport.requests),
        )
        return public_error
    finally:
        del provider_error, transport, request, public_error


@pytest.mark.asyncio
async def test_ordinary_failure_retains_no_request_transport_or_private_content() -> (
    None
):
    public_error = await _ordinary_failure_privacy_probe()
    capture = _privacy_capture
    assert capture is not None
    assert type(public_error) is KisIncompleteInstrumentMasterDownload
    assert public_error.args == ("KIS KRX instrument master download is incomplete",)
    assert public_error.__cause__ is None
    assert public_error.__context__ is None
    assert capture.request_count == 1
    reachable = tuple(_error_reachable_values(public_error))
    assert any(isinstance(value, FrameType) for value in reachable)
    assert all(
        all(value is not forbidden for value in reachable)
        for forbidden in capture.forbidden
    )
    assert all(
        not _contains_private_content(value, capture.private_contents)
        for value in reachable
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control",
    [
        asyncio.CancelledError("private"),
        KeyboardInterrupt("private"),
        SystemExit("private"),
    ],
)
async def test_control_failures_propagate_same_sanitized_object(
    control: BaseException,
) -> None:
    transport = ScriptedTransport((control,))

    with pytest.raises(type(control)) as raised:
        await download_kis_krx_common_stock_authority(
            transport=transport,
            clock=lambda: DOWNLOADED_AT,
        )

    assert raised.value is control
    assert raised.value.args == ((1,) if isinstance(control, SystemExit) else ())
    if isinstance(raised.value, SystemExit):
        assert raised.value.code == 1
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__suppress_context__
    product_frames: list[FrameType] = []
    traceback = raised.value.__traceback__
    while traceback is not None:
        if Path(traceback.tb_frame.f_code.co_filename).name == (
            "instrument_master_source.py"
        ):
            product_frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert [frame.f_code.co_name for frame in product_frames] == [
        "download_kis_krx_common_stock_authority"
    ]
    assert all(
        id(transport) not in {id(value) for value in frame.f_locals.values()}
        for frame in product_frames
    )
    assert all(
        not _contains_private_content(value, ("private",))
        for value in _error_reachable_values(raised.value)
    )


@pytest.mark.asyncio
async def test_master_transport_bounds_the_opener_read() -> None:
    response = FakeHttpResponse(body=b"x" * (4 * 1024 * 1024 + 2))
    opener = RecordingOpener(response=response)
    transport = KisInstrumentMasterHttpsTransport(opener=opener)

    with pytest.raises(BrokerTransportError, match="size"):
        await transport.request(BrokerRequest(method="GET", path=KOSPI_PATH))

    assert response.read_amounts == [4 * 1024 * 1024 + 1]


def _error_reachable_values(error: BaseException) -> Iterator[object]:
    pending: list[object] = [error]
    visited: set[int] = set()
    while pending and len(visited) < 750:
        value = pending.pop()
        if value is None or id(value) in visited:
            continue
        visited.add(id(value))
        yield value
        if isinstance(value, BaseException):
            pending.extend(value.args)
            pending.extend((value.__cause__, value.__context__, value.__traceback__))
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
                slots = (raw_slots,) if isinstance(raw_slots, str) else raw_slots
                if not isinstance(slots, tuple):
                    continue
                for slot in cast(tuple[object, ...], slots):
                    if isinstance(slot, str) and hasattr(value, slot):
                        pending.append(getattr(value, slot))


def _contains_private_content(value: object, contents: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(content in value for content in contents)
    if isinstance(value, bytes):
        return any(content.encode("utf-8") in value for content in contents)
    renderer = reprlib.Repr()
    renderer.maxother = 1_024
    renderer.maxstring = 1_024
    try:
        rendered = renderer.repr(value)
    except Exception:
        return False
    return any(content in rendered for content in contents)


@pytest.mark.asyncio
async def test_master_https_transport_uses_only_exact_query_free_gets() -> None:
    opener = RecordingOpener()
    transport = KisInstrumentMasterHttpsTransport(opener=opener)

    await transport.request(BrokerRequest(method="GET", path=KOSPI_PATH))
    await transport.request(BrokerRequest(method="GET", path=KOSDAQ_PATH))
    for request in (
        BrokerRequest(method="POST", path=KOSPI_PATH),
        BrokerRequest(method="GET", path=f"{KOSPI_PATH}?x=1"),
        BrokerRequest(method="GET", path="/common/master/./kospi_code.mst.zip"),
        BrokerRequest(method="GET", path="/common/master/nested/kospi_code.mst.zip"),
    ):
        with pytest.raises(BrokerWriteDisabled, match="not allowed"):
            await transport.request(request)

    assert [request.full_url for request in opener.requests] == [
        f"https://new.real.download.dws.co.kr{KOSPI_PATH}",
        f"https://new.real.download.dws.co.kr{KOSDAQ_PATH}",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broker_request",
    [
        BrokerRequest(
            method="GET",
            path=KOSPI_PATH,
            headers=(("Authorization", "Bearer private"),),
        ),
        BrokerRequest(method="GET", path=KOSPI_PATH, body=b"private"),
    ],
    ids=("credential-header", "request-body"),
)
async def test_master_transport_rejects_headers_and_body_before_opener(
    broker_request: BrokerRequest,
) -> None:
    opener = RecordingOpener()
    transport = KisInstrumentMasterHttpsTransport(opener=opener)

    with pytest.raises(BrokerWriteDisabled, match="not allowed"):
        await transport.request(broker_request)

    assert opener.requests == []
