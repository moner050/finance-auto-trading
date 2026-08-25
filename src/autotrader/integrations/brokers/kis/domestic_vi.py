from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, NoReturn, cast
from urllib.parse import urlencode

from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)

if TYPE_CHECKING:
    from autotrader.integrations.brokers.kis.adapter import KisReadCredentials

KisViEvidenceRecord = tuple[tuple[str, str], ...]
_INCOMPLETE = "KIS domestic VI snapshot is incomplete"
_PATH = "/uapi/domestic-stock/v1/quotations/inquire-vi-status"
_TR_ID = "FHPST01390000"
_INVALID_MARKET = "KIS domestic VI market is invalid"
_INVALID_SYMBOL = "KIS domestic VI symbol is invalid"
_INVALID_DATE = "KIS domestic VI date is invalid"
_INVALID_PAGE_LIMIT = "KIS domestic VI page limit is invalid"


class KisViMarket(StrEnum):
    KOSPI = "KOSPI"
    KOSDAQ = "KOSDAQ"


class KisIncompleteDomesticViSnapshot(RuntimeError):
    """Raised when KIS VI evidence cannot form a complete snapshot."""


@dataclass(frozen=True, slots=True)
class KisDomesticViEvidencePage:
    records: tuple[KisViEvidenceRecord, ...]

    def __post_init__(self) -> None:
        records = cast(object, self.records)
        if not isinstance(records, tuple) or not all(
            _evidence_record(record) for record in cast(tuple[object, ...], records)
        ):
            raise ValueError("KIS domestic VI evidence must be immutable")


@dataclass(frozen=True, slots=True)
class KisDomesticViEvidenceSnapshot:
    pages: tuple[KisDomesticViEvidencePage, ...]

    def __post_init__(self) -> None:
        pages = cast(object, self.pages)
        if not isinstance(pages, tuple) or not all(
            isinstance(page, KisDomesticViEvidencePage)
            for page in cast(tuple[object, ...], pages)
        ):
            raise ValueError("KIS domestic VI pages must be an immutable tuple")


class KisDomesticViReadOnlyAdapter:
    """Reads KIS VI response evidence without assigning a VI state."""

    def __init__(self, *, transport: AsyncHttpTransport) -> None:
        self._transport = transport

    async def read_complete_snapshot(
        self,
        *,
        credentials: KisReadCredentials,
        market: KisViMarket,
        symbol: str,
        business_date: date,
        max_pages: int,
    ) -> KisDomesticViEvidenceSnapshot:
        transport = self._transport
        try:
            outcome = await _read_snapshot(
                transport=transport,
                credentials=credentials,
                market=market,
                symbol=symbol,
                business_date=business_date,
                max_pages=max_pages,
            )
        finally:
            del self, transport, credentials, market, symbol, business_date, max_pages
        value_error, error_message, snapshot = outcome
        del outcome
        if error_message is not None:
            del snapshot
            if value_error:
                raise ValueError(error_message)
            raise KisIncompleteDomesticViSnapshot(error_message)
        return snapshot

    async def submit(self, *, command: object) -> NoReturn:
        del self, command
        raise BrokerWriteDisabled("KIS domestic VI write adapter is not enabled")

    async def cancel(self, *, command: object) -> NoReturn:
        del self, command
        raise BrokerWriteDisabled("KIS domestic VI write adapter is not enabled")

    async def replace(self, *, command: object) -> NoReturn:
        del self, command
        raise BrokerWriteDisabled("KIS domestic VI write adapter is not enabled")


async def _read_snapshot(
    *,
    transport: AsyncHttpTransport,
    credentials: KisReadCredentials,
    market: KisViMarket,
    symbol: str,
    business_date: date,
    max_pages: int,
) -> tuple[bool, str | None, KisDomesticViEvidenceSnapshot]:
    fallback = KisDomesticViEvidenceSnapshot(pages=())
    request: BrokerRequest | None = None
    response: BrokerResponse | None = None
    page: KisDomesticViEvidencePage | None = None
    pages: list[KisDomesticViEvidencePage] = []
    try:
        validation_error = _validation_error(market, symbol, business_date, max_pages)
        if validation_error is not None:
            return True, validation_error, fallback
        for page_number in range(max_pages):
            request = _request(credentials, market, symbol, business_date)
            if page_number:
                request = _continued_request(request)
            response = await transport.request(request)
            continuation = response.header("tr_cont")
            page = _decode_page(response)
            response = None
            request = None
            if page is None or page in pages:
                return False, _INCOMPLETE, fallback
            pages.append(page)
            page = None
            if continuation in {None, "", "N"}:
                return False, None, KisDomesticViEvidenceSnapshot(pages=tuple(pages))
            if continuation != "M":
                return False, _INCOMPLETE, fallback
        return False, _INCOMPLETE, fallback
    except Exception:
        return False, _INCOMPLETE, fallback
    finally:
        del (
            transport,
            credentials,
            market,
            symbol,
            business_date,
            max_pages,
            request,
            response,
            page,
            pages,
        )


def _validation_error(
    market: object, symbol: object, business_date: object, max_pages: object
) -> str | None:
    if type(market) is not KisViMarket:
        return _INVALID_MARKET
    if (
        not isinstance(symbol, str)
        or len(symbol) != 6
        or not symbol.isascii()
        or not symbol.isdigit()
    ):
        return _INVALID_SYMBOL
    if type(business_date) is not date:
        return _INVALID_DATE
    if type(max_pages) is not int or not 1 <= max_pages <= 10:
        return _INVALID_PAGE_LIMIT
    return None


def _request(
    credentials: KisReadCredentials,
    market: KisViMarket,
    symbol: str,
    business_date: date,
) -> BrokerRequest:
    market_code = "K" if market is KisViMarket.KOSPI else "Q"
    query = urlencode(
        (
            ("FID_DIV_CLS_CODE", "0"),
            ("FID_COND_SCR_DIV_CODE", "20139"),
            ("FID_MRKT_CLS_CODE", market_code),
            ("FID_INPUT_ISCD", symbol),
            ("FID_RANK_SORT_CLS_CODE", "0"),
            ("FID_INPUT_DATE_1", business_date.strftime("%Y%m%d")),
            ("FID_TRGT_CLS_CODE", ""),
            ("FID_TRGT_EXLS_CLS_CODE", ""),
        )
    )
    return BrokerRequest(
        method="GET",
        path=f"{_PATH}?{query}",
        headers=(
            ("authorization", f"Bearer {credentials.access_token}"),
            ("appkey", credentials.app_key),
            ("appsecret", credentials.app_secret),
            ("tr_id", _TR_ID),
            ("custtype", "P"),
        ),
    )


def _continued_request(request: BrokerRequest) -> BrokerRequest:
    return BrokerRequest(
        method=request.method,
        path=request.path,
        headers=(*request.headers, ("tr_cont", "N")),
    )


def _decode_page(response: BrokerResponse) -> KisDomesticViEvidencePage | None:
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
        output = typed_payload.get("output")
        if typed_payload.get("rt_cd") != "0" or not isinstance(output, list):
            return None
        return KisDomesticViEvidencePage(
            records=tuple(
                _normalize_record(cast(Mapping[object, object], record))
                for record in cast(list[object], output)
            )
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


def _normalize_record(value: object) -> KisViEvidenceRecord:
    if not isinstance(value, Mapping):
        raise ValueError
    normalized = tuple(sorted(cast(Mapping[object, object], value).items()))
    if not _evidence_record(normalized):
        raise ValueError
    return cast(KisViEvidenceRecord, normalized)


def _evidence_record(value: object) -> bool:
    if not isinstance(value, tuple) or not value:
        return False
    for pair in cast(tuple[object, ...], value):
        if not isinstance(pair, tuple):
            return False
        raw_pair = cast(tuple[object, ...], pair)
        if (
            len(raw_pair) != 2
            or not _single_line_text(raw_pair[0])
            or not _single_line_text(raw_pair[1])
        ):
            return False
    return True


def _single_line_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "\n" not in value
        and "\r" not in value
    )
