from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

from autotrader.domain.krx_instrument_authority import (
    KrxCommonStockAuthoritySnapshot,
)
from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
    HttpOpener,
    WhitelistedHttpsTransport,
)
from autotrader.integrations.brokers.kis.instrument_master import (
    parse_kis_krx_common_stock_authority,
)

_KOSPI_PATH = "/common/master/kospi_code.mst.zip"
_KOSDAQ_PATH = "/common/master/kosdaq_code.mst.zip"
_MASTER_ROUTES = frozenset({("GET", _KOSPI_PATH), ("GET", _KOSDAQ_PATH)})
_MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
_MAX_MEMBER_BYTES = 8 * 1024 * 1024
_SUPPORTED_COMPRESSION = {ZIP_STORED, ZIP_DEFLATED}
_REGULAR_FILE_MODE = 0o100000
_FILE_TYPE_MASK = 0o170000


class KisIncompleteInstrumentMasterDownload(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KisDownloadedKrxCommonStockAuthority:
    snapshot: KrxCommonStockAuthoritySnapshot
    source_last_modified_at: datetime
    downloaded_at: datetime

    def __post_init__(self) -> None:
        if type(self.snapshot) is not KrxCommonStockAuthoritySnapshot:
            raise ValueError("snapshot must have the exact authority type")
        try:
            self.snapshot.__post_init__()
        except (TypeError, ValueError) as error:
            raise ValueError("snapshot invariant is invalid") from error
        _require_whole_second_utc(
            self.source_last_modified_at, "source_last_modified_at"
        )
        _require_whole_second_utc(self.downloaded_at, "downloaded_at")
        if self.snapshot.captured_at != self.source_last_modified_at:
            raise ValueError("snapshot must bind the provider source time")
        if self.source_last_modified_at > self.downloaded_at:
            raise ValueError("source time must not be after download time")


class KisInstrumentMasterHttpsTransport(WhitelistedHttpsTransport):
    def __init__(self, *, opener: HttpOpener | None = None) -> None:
        super().__init__(
            base_url="https://new.real.download.dws.co.kr",
            allowed_routes=_MASTER_ROUTES,
            opener=opener,
            max_response_bytes=_MAX_ARCHIVE_BYTES,
        )

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        if (
            request.method != "GET"
            or request.path not in {_KOSPI_PATH, _KOSDAQ_PATH}
            or request.headers
            or request.body is not None
        ):
            raise BrokerWriteDisabled("KIS instrument master route is not allowed")
        return await super().request(request)


async def download_kis_krx_common_stock_authority(
    *,
    transport: AsyncHttpTransport,
    clock: Callable[[], datetime] | None = None,
) -> KisDownloadedKrxCommonStockAuthority:
    try:
        result = await _attempt_download(
            transport=transport,
            clock=_utc_now if clock is None else clock,
        )
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as control:
        _sanitize_control(control)
        del transport, clock
        raise control.with_traceback(None) from None
    del transport, clock
    if result is None:
        raise KisIncompleteInstrumentMasterDownload(
            "KIS KRX instrument master download is incomplete"
        ) from None
    return result


async def _attempt_download(
    *,
    transport: AsyncHttpTransport,
    clock: Callable[[], datetime],
) -> KisDownloadedKrxCommonStockAuthority | None:
    try:
        return await _download(transport=transport, clock=clock)
    except Exception:
        return None


async def _download(
    *,
    transport: AsyncHttpTransport,
    clock: Callable[[], datetime],
) -> KisDownloadedKrxCommonStockAuthority:
    kospi_response = await transport.request(
        BrokerRequest(method="GET", path=_KOSPI_PATH)
    )
    kospi_master, kospi_modified = _validated_member(
        response=kospi_response,
        expected_member="kospi_code.mst",
    )
    kosdaq_response = await transport.request(
        BrokerRequest(method="GET", path=_KOSDAQ_PATH)
    )
    kosdaq_master, kosdaq_modified = _validated_member(
        response=kosdaq_response,
        expected_member="kosdaq_code.mst",
    )
    if kospi_modified != kosdaq_modified:
        raise ValueError("master source times do not match")
    downloaded_at = clock()
    snapshot = parse_kis_krx_common_stock_authority(
        kospi_master,
        kosdaq_master,
        captured_at=kospi_modified,
    )
    return KisDownloadedKrxCommonStockAuthority(
        snapshot=snapshot,
        source_last_modified_at=kospi_modified,
        downloaded_at=downloaded_at,
    )


def _validated_member(
    *,
    response: BrokerResponse,
    expected_member: str,
) -> tuple[bytes, datetime]:
    if type(response) is not BrokerResponse or response.status != 200:
        raise ValueError("master response must be an exact success")
    if not response.body or len(response.body) > _MAX_ARCHIVE_BYTES:
        raise ValueError("master archive size is invalid")
    content_types = [
        value for name, value in response.headers if name.casefold() == "content-type"
    ]
    if (
        len(content_types) != 1
        or content_types[0].partition(";")[0].strip().casefold() != "application/zip"
    ):
        raise ValueError("master response content type is invalid")
    modified_values = [
        value for name, value in response.headers if name.casefold() == "last-modified"
    ]
    if len(modified_values) != 1:
        raise ValueError("master response source time is missing")
    source_modified_at = parsedate_to_datetime(modified_values[0])
    _require_whole_second_utc(source_modified_at, "source_last_modified_at")
    member = _read_exact_zip_member(response.body, expected_member=expected_member)
    return member, source_modified_at


def _read_exact_zip_member(archive_body: bytes, *, expected_member: str) -> bytes:
    try:
        with ZipFile(BytesIO(archive_body)) as archive:
            members = archive.infolist()
            if len(members) != 1:
                raise ValueError("master archive must have one member")
            member = members[0]
            unix_mode = member.external_attr >> 16
            file_type = unix_mode & _FILE_TYPE_MASK
            if (
                member.filename != expected_member
                or member.is_dir()
                or member.flag_bits & 1
                or member.compress_type not in _SUPPORTED_COMPRESSION
                or file_type not in {0, _REGULAR_FILE_MODE}
                or member.file_size <= 0
                or member.file_size > _MAX_MEMBER_BYTES
                or member.compress_size > _MAX_ARCHIVE_BYTES
            ):
                raise ValueError("master archive member is invalid")
            body = archive.read(member)
    except (BadZipFile, OSError, RuntimeError) as error:
        raise ValueError("master archive is invalid") from error
    if len(body) != member.file_size:
        raise ValueError("master archive member length is invalid")
    return body


def _require_whole_second_utc(value: object, field: str) -> None:
    if type(value) is not datetime or value.tzinfo is not UTC or value.microsecond != 0:
        raise ValueError(f"{field} must be a whole-second exact UTC datetime")


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _sanitize_control(error: BaseException) -> None:
    if isinstance(error, SystemExit):
        error.code = 1
        error.args = (1,)
    else:
        error.args = ()
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True
    error.__traceback__ = None
    error.__dict__.clear()
