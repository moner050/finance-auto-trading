from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import NoReturn, cast
from urllib.parse import urlencode

from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)
from autotrader.integrations.brokers.kis.adapter import KisReadCredentials

KisProviderEvidenceRecord = tuple[tuple[str, str], ...]
_INCOMPLETE = "KIS domestic index snapshot is incomplete"
_CATEGORY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-index-category-price"
_CATEGORY_TR_ID = "FHPUP02140000"
_DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-index-daily-price"
_DAILY_TR_ID = "FHPUP02120000"
_CONTINUATION_HEADERS = frozenset(("M", "F"))
_INVALID_INDEX_CODE = "KIS domestic index code is invalid"
_INVALID_START_DATE = "KIS domestic index start date is invalid"
_INVALID_PAGE_LIMIT = "KIS domestic index page limit is invalid"


class KisIncompleteDomesticIndexSnapshot(RuntimeError):
    """Raised when KIS category evidence cannot form a complete snapshot."""


@dataclass(frozen=True, slots=True)
class KisDomesticIndexEvidencePage:
    output1: KisProviderEvidenceRecord
    output2: tuple[KisProviderEvidenceRecord, ...]

    def __post_init__(self) -> None:
        output1 = cast(object, self.output1)
        output2 = cast(object, self.output2)
        if not _evidence_record(output1) or not isinstance(output2, tuple):
            raise ValueError("KIS domestic index evidence must be immutable")
        records = cast(tuple[object, ...], output2)
        if not all(_evidence_record(record) for record in records):
            raise ValueError("KIS domestic index evidence must be immutable")


@dataclass(frozen=True, slots=True)
class KisDomesticIndexEvidenceSnapshot:
    pages: tuple[KisDomesticIndexEvidencePage, ...]

    def __post_init__(self) -> None:
        pages = cast(object, self.pages)
        if not isinstance(pages, tuple) or not all(
            isinstance(page, KisDomesticIndexEvidencePage)
            for page in cast(tuple[object, ...], pages)
        ):
            raise ValueError("KIS domestic index pages must be an immutable tuple")


class KisDomesticIndexReadOnlyAdapter:
    """Reads KIS domestic-index source evidence without provider interpretation."""

    def __init__(self, *, transport: AsyncHttpTransport) -> None:
        self._transport = transport

    async def read_complete_category_snapshot(
        self, *, credentials: KisReadCredentials, max_pages: int
    ) -> KisDomesticIndexEvidenceSnapshot:
        transport = self._transport
        try:
            outcome = await _read_category_snapshot(
                transport=transport,
                credentials=credentials,
                max_pages=max_pages,
            )
        finally:
            del self, transport, credentials, max_pages
        value_error, error_message, snapshot = outcome
        del outcome
        if error_message is not None:
            del snapshot
            if value_error:
                raise ValueError(error_message)
            raise KisIncompleteDomesticIndexSnapshot(error_message)
        return snapshot

    async def read_complete_daily_snapshot(
        self,
        *,
        credentials: KisReadCredentials,
        index_code: str,
        start_date: date,
        max_pages: int,
    ) -> KisDomesticIndexEvidenceSnapshot:
        transport = self._transport
        try:
            outcome = await _read_daily_snapshot(
                transport=transport,
                credentials=credentials,
                index_code=index_code,
                start_date=start_date,
                max_pages=max_pages,
            )
        finally:
            del (
                self,
                transport,
                credentials,
                index_code,
                start_date,
                max_pages,
            )
        value_error, error_message, snapshot = outcome
        del outcome
        if error_message is not None:
            del snapshot
            if value_error:
                raise ValueError(error_message)
            raise KisIncompleteDomesticIndexSnapshot(error_message)
        return snapshot

    async def submit(self, *, command: object) -> NoReturn:
        del self, command
        raise BrokerWriteDisabled("KIS domestic index write adapter is not enabled")

    async def cancel(self, *, command: object) -> NoReturn:
        del self, command
        raise BrokerWriteDisabled("KIS domestic index write adapter is not enabled")

    async def replace(self, *, command: object) -> NoReturn:
        del self, command
        raise BrokerWriteDisabled("KIS domestic index write adapter is not enabled")


async def _read_complete_snapshot(
    *,
    transport: AsyncHttpTransport,
    request_builder: Callable[[], BrokerRequest],
    page_decoder: Callable[[BrokerResponse], KisDomesticIndexEvidencePage | None],
    max_pages: int,
) -> tuple[str | None, KisDomesticIndexEvidenceSnapshot]:
    fallback = KisDomesticIndexEvidenceSnapshot(pages=())
    pages: list[KisDomesticIndexEvidencePage] = []
    request: BrokerRequest | None = None
    response: BrokerResponse | None = None
    page: KisDomesticIndexEvidencePage | None = None
    try:
        continuation = False
        for _ in range(max_pages):
            request = request_builder()
            if continuation:
                request = _continued_request(request)
            response = await transport.request(request)
            page = page_decoder(response)
            continues = response.header("tr_cont") in _CONTINUATION_HEADERS
            response = None
            request = None
            if page is None or page in pages:
                return _INCOMPLETE, fallback
            pages.append(page)
            page = None
            if not continues:
                return None, KisDomesticIndexEvidenceSnapshot(pages=tuple(pages))
            continuation = True
        return _INCOMPLETE, fallback
    finally:
        del transport, request_builder, pages, request, response, page


async def _read_category_snapshot(
    *,
    transport: AsyncHttpTransport,
    credentials: KisReadCredentials,
    max_pages: int,
) -> tuple[bool, str | None, KisDomesticIndexEvidenceSnapshot]:
    fallback = KisDomesticIndexEvidenceSnapshot(pages=())
    page_limit: int | None = None
    request_builder: Callable[[], BrokerRequest] | None = None
    try:
        try:
            page_limit = _page_limit(max_pages)
        except ValueError:
            return True, _INVALID_PAGE_LIMIT, fallback

        request_builder = _category_request_builder(credentials)

        error_message, snapshot = await _read_complete_snapshot(
            transport=transport,
            request_builder=request_builder,
            page_decoder=_decode_evidence_page,
            max_pages=page_limit,
        )
        return False, error_message, snapshot
    except Exception:
        return False, _INCOMPLETE, fallback
    finally:
        del transport, credentials, max_pages, page_limit, request_builder


async def _read_daily_snapshot(
    *,
    transport: AsyncHttpTransport,
    credentials: KisReadCredentials,
    index_code: str,
    start_date: date,
    max_pages: int,
) -> tuple[bool, str | None, KisDomesticIndexEvidenceSnapshot]:
    fallback = KisDomesticIndexEvidenceSnapshot(pages=())
    page_limit: int | None = None
    request_builder: Callable[[], BrokerRequest] | None = None
    try:
        if len(index_code) != 4 or not index_code.isascii() or not index_code.isdigit():
            return True, _INVALID_INDEX_CODE, fallback
        if type(start_date) is not date:
            return True, _INVALID_START_DATE, fallback
        try:
            page_limit = _page_limit(max_pages)
        except ValueError:
            return True, _INVALID_PAGE_LIMIT, fallback

        request_builder = _daily_request_builder(credentials, index_code, start_date)

        error_message, snapshot = await _read_complete_snapshot(
            transport=transport,
            request_builder=request_builder,
            page_decoder=_decode_evidence_page,
            max_pages=page_limit,
        )
        return False, error_message, snapshot
    except Exception:
        return False, _INCOMPLETE, fallback
    finally:
        del (
            transport,
            credentials,
            index_code,
            start_date,
            max_pages,
            page_limit,
            request_builder,
        )


def _category_request(credentials: KisReadCredentials) -> BrokerRequest:
    query = urlencode(
        (
            ("FID_COND_MRKT_DIV_CODE", "U"),
            ("FID_INPUT_ISCD", "0001"),
            ("FID_COND_SCR_DIV_CODE", "20214"),
            ("FID_MRKT_CLS_CODE", "K"),
            ("FID_BLNG_CLS_CODE", "0"),
        )
    )
    return BrokerRequest(
        method="GET",
        path=f"{_CATEGORY_PATH}?{query}",
        headers=_headers(credentials, _CATEGORY_TR_ID),
    )


def _category_request_builder(
    credentials: KisReadCredentials,
) -> Callable[[], BrokerRequest]:
    def request_builder() -> BrokerRequest:
        return _category_request(credentials)

    return request_builder


def _daily_request(
    credentials: KisReadCredentials, index_code: str, start_date: date
) -> BrokerRequest:
    query = urlencode(
        (
            ("FID_PERIOD_DIV_CODE", "D"),
            ("FID_COND_MRKT_DIV_CODE", "U"),
            ("FID_INPUT_ISCD", index_code),
            ("FID_INPUT_DATE_1", start_date.strftime("%Y%m%d")),
        )
    )
    return BrokerRequest(
        method="GET",
        path=f"{_DAILY_PATH}?{query}",
        headers=_headers(credentials, _DAILY_TR_ID),
    )


def _daily_request_builder(
    credentials: KisReadCredentials,
    index_code: str,
    start_date: date,
) -> Callable[[], BrokerRequest]:
    def request_builder() -> BrokerRequest:
        return _daily_request(credentials, index_code, start_date)

    return request_builder


def _continued_request(request: BrokerRequest) -> BrokerRequest:
    return BrokerRequest(
        method=request.method,
        path=request.path,
        headers=(*request.headers, ("tr_cont", "N")),
    )


def _page_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 10:
        raise ValueError(_INVALID_PAGE_LIMIT)
    return value


def _headers(
    credentials: KisReadCredentials, tr_id: str
) -> tuple[tuple[str, str], ...]:
    return (
        ("authorization", f"Bearer {credentials.access_token}"),
        ("appkey", credentials.app_key),
        ("appsecret", credentials.app_secret),
        ("tr_id", tr_id),
        ("custtype", "P"),
    )


def _decode_evidence_page(
    response: BrokerResponse,
) -> KisDomesticIndexEvidencePage | None:
    status = response.status
    body = response.body
    del response
    try:
        if status != 200:
            return None
        payload: object = json.loads(body)
        if not isinstance(payload, Mapping):
            return None
        typed_payload = cast(Mapping[str, object], payload)
        if typed_payload.get("rt_cd") != "0":
            return None
        output1 = typed_payload.get("output1")
        output2 = typed_payload.get("output2")
        if not isinstance(output1, Mapping) or not isinstance(output2, list):
            return None
        return KisDomesticIndexEvidencePage(
            output1=_normalize_record(cast(Mapping[object, object], output1)),
            output2=tuple(
                _normalize_record(cast(Mapping[object, object], record))
                for record in cast(list[object], output2)
            ),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        return None
    finally:
        del body


def _normalize_record(value: object) -> KisProviderEvidenceRecord:
    if not isinstance(value, Mapping):
        raise ValueError
    normalized = tuple(sorted(cast(Mapping[object, object], value).items()))
    if not _evidence_record(normalized):
        raise ValueError
    return cast(KisProviderEvidenceRecord, normalized)


def _evidence_record(value: object) -> bool:
    if not isinstance(value, tuple) or not value:
        return False
    for pair in cast(tuple[object, ...], value):
        if not isinstance(pair, tuple):
            return False
        raw_pair = cast(tuple[object, ...], pair)
        if (
            len(raw_pair) != 2
            or not _nonempty_single_line_text(raw_pair[0])
            or not _single_line_string(raw_pair[1])
        ):
            return False
    return True


def _nonempty_single_line_text(value: object) -> bool:
    return _single_line_string(value) and bool(value)


def _single_line_string(value: object) -> bool:
    return isinstance(value, str) and "\n" not in value and "\r" not in value
