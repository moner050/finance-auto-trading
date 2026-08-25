from __future__ import annotations

import gzip
import importlib
import importlib.util
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from io import BytesIO
from typing import Protocol, cast
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from autotrader.integrations.brokers.common import (
    BrokerRequest,
    BrokerResponse,
    BrokerTransportError,
    BrokerWriteDisabled,
    HttpOpener,
)
from autotrader.integrations.brokers.toss.rate_limit import (
    TossRateLimitedTransport,
)


@dataclass
class FakeHttpResponse:
    status: int = 200
    body: bytes = b"{}"
    headers: Mapping[str, str] = field(default_factory=dict[str, str])

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        del amount
        return self.body


@dataclass
class RecordingOpener:
    requests: list[Request] = field(default_factory=list[Request])
    response: FakeHttpResponse = field(default_factory=FakeHttpResponse)

    def __call__(self, request: Request, timeout: float) -> FakeHttpResponse:
        del timeout
        self.requests.append(request)
        return self.response


@dataclass
class ErrorOpener:
    error: HTTPError
    requests: list[Request] = field(default_factory=list[Request])

    def __call__(self, request: Request, timeout: float) -> FakeHttpResponse:
        del timeout
        self.requests.append(request)
        raise self.error


class _BrokerHttpsTransport(Protocol):
    def __init__(self, *, opener: HttpOpener) -> None: ...

    async def request(self, request: BrokerRequest) -> BrokerResponse: ...


def _transport_type(module_name: str, name: str) -> type[_BrokerHttpsTransport] | None:
    if importlib.util.find_spec(module_name) is None:
        return None
    transport_type = getattr(importlib.import_module(module_name), name, None)
    if not isinstance(transport_type, type):
        return None
    return cast(type[_BrokerHttpsTransport], transport_type)


@pytest.mark.asyncio
async def test_toss_https_transport_uses_only_the_toss_api_origin() -> None:
    transport_type = _transport_type(
        "autotrader.integrations.brokers.toss.transport", "TossHttpsTransport"
    )
    assert transport_type is not None
    opener = RecordingOpener()
    transport = transport_type(opener=opener)

    await transport.request(
        BrokerRequest(method="GET", path="/api/v1/prices?symbols=AAPL")
    )

    assert opener.requests[0].full_url == (
        "https://openapi.tossinvest.com/api/v1/prices?symbols=AAPL"
    )


@pytest.mark.asyncio
async def test_toss_rate_limiter_composes_with_the_https_transport() -> None:
    transport_type = _transport_type(
        "autotrader.integrations.brokers.toss.transport", "TossHttpsTransport"
    )
    assert transport_type is not None
    opener = RecordingOpener(
        response=FakeHttpResponse(
            headers={
                "X-RateLimit-Limit": "1",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1",
            }
        )
    )

    async def sleep(_seconds: float) -> None:
        return None

    transport = TossRateLimitedTransport(
        transport=transport_type(opener=opener),
        monotonic=lambda: 0.0,
        sleep=sleep,
        wall_clock=lambda: datetime(2026, 8, 19, tzinfo=UTC),
        jitter=lambda _: 0.0,
        deadline=18.0,
    )

    await transport.request(
        BrokerRequest(
            method="GET",
            path="/api/v1/accounts",
            headers=(("Authorization", "Bearer private"),),
        )
    )

    assert opener.requests[0].full_url == (
        "https://openapi.tossinvest.com/api/v1/accounts"
    )


@pytest.mark.asyncio
async def test_toss_https_transport_decodes_gzip_response_body() -> None:
    transport_type = _transport_type(
        "autotrader.integrations.brokers.toss.transport", "TossHttpsTransport"
    )
    assert transport_type is not None
    body = b'{"result":{"candles":[]}}'
    opener = RecordingOpener(
        response=FakeHttpResponse(
            body=gzip.compress(body), headers={"Content-Encoding": "gzip"}
        )
    )
    transport = transport_type(opener=opener)

    response = await transport.request(
        BrokerRequest(method="GET", path="/api/v1/candles?symbol=005930")
    )

    assert response.body == body


@pytest.mark.asyncio
async def test_toss_https_transport_decodes_gzip_http_error_body() -> None:
    transport_type = _transport_type(
        "autotrader.integrations.brokers.toss.transport", "TossHttpsTransport"
    )
    assert transport_type is not None
    body = b'{"code":"invalid-request"}'
    headers = Message()
    headers["Content-Encoding"] = "gzip"
    opener = ErrorOpener(
        error=HTTPError(
            url="https://openapi.tossinvest.com/api/v1/candles",
            code=400,
            msg="Bad Request",
            hdrs=headers,
            fp=BytesIO(gzip.compress(body)),
        )
    )
    transport = transport_type(opener=opener)

    response = await transport.request(
        BrokerRequest(method="GET", path="/api/v1/candles?symbol=005930")
    )

    assert response == BrokerResponse(
        status=400, body=body, headers=(("Content-Encoding", "gzip"),)
    )


@pytest.mark.asyncio
async def test_toss_https_transport_fails_closed_on_corrupt_gzip_body() -> None:
    transport_type = _transport_type(
        "autotrader.integrations.brokers.toss.transport", "TossHttpsTransport"
    )
    assert transport_type is not None
    malformed = bytearray(gzip.compress(b'{"result":{}}'))
    malformed[10] ^= 0xFF
    opener = RecordingOpener(
        response=FakeHttpResponse(
            body=bytes(malformed), headers={"Content-Encoding": "gzip"}
        )
    )
    transport = transport_type(opener=opener)

    with pytest.raises(BrokerTransportError, match="body is invalid"):
        await transport.request(
            BrokerRequest(method="GET", path="/api/v1/candles?symbol=005930")
        )


@pytest.mark.asyncio
async def test_toss_https_transport_allows_krw_buying_power_but_blocks_writes() -> None:
    transport_type = _transport_type(
        "autotrader.integrations.brokers.toss.transport", "TossHttpsTransport"
    )
    assert transport_type is not None
    opener = RecordingOpener()
    transport = transport_type(opener=opener)

    await transport.request(
        BrokerRequest(method="GET", path="/api/v1/buying-power?currency=KRW")
    )
    with pytest.raises(BrokerWriteDisabled, match="not allowed"):
        await transport.request(
            BrokerRequest(method="POST", path="/api/v1/buying-power")
        )

    assert [request.full_url for request in opener.requests] == [
        "https://openapi.tossinvest.com/api/v1/buying-power?currency=KRW"
    ]


@pytest.mark.asyncio
async def test_toss_https_transport_allows_only_sellable_quantity_get() -> None:
    transport_type = _transport_type(
        "autotrader.integrations.brokers.toss.transport", "TossHttpsTransport"
    )
    assert transport_type is not None
    opener = RecordingOpener()
    transport = transport_type(opener=opener)

    await transport.request(
        BrokerRequest(method="GET", path="/api/v1/sellable-quantity?symbol=005930")
    )
    with pytest.raises(BrokerWriteDisabled, match="not allowed"):
        await transport.request(
            BrokerRequest(method="POST", path="/api/v1/sellable-quantity")
        )
    with pytest.raises(BrokerWriteDisabled, match="not allowed"):
        await transport.request(
            BrokerRequest(method="GET", path="/api/v1/sellable-quantity/005930")
        )

    assert [request.full_url for request in opener.requests] == [
        "https://openapi.tossinvest.com/api/v1/sellable-quantity?symbol=005930"
    ]


@pytest.mark.asyncio
async def test_toss_https_transport_allows_individual_order_reads() -> None:
    transport_type = _transport_type(
        "autotrader.integrations.brokers.toss.transport", "TossHttpsTransport"
    )
    assert transport_type is not None
    opener = RecordingOpener()
    transport = transport_type(opener=opener)

    await transport.request(
        BrokerRequest(method="GET", path="/api/v1/orders/opaque-order-id")
    )

    assert opener.requests[0].full_url == (
        "https://openapi.tossinvest.com/api/v1/orders/opaque-order-id"
    )


@pytest.mark.asyncio
async def test_toss_https_transport_allows_one_opaque_stock_warning_segment() -> None:
    transport_type = _transport_type(
        "autotrader.integrations.brokers.toss.transport", "TossHttpsTransport"
    )
    assert transport_type is not None
    opener = RecordingOpener()
    transport = transport_type(opener=opener)

    assert (
        await transport.request(
            BrokerRequest(method="GET", path="/api/v1/stocks/005930/warnings")
        )
    ).status == 200

    assert opener.requests[0].full_url == (
        "https://openapi.tossinvest.com/api/v1/stocks/005930/warnings"
    )


@pytest.mark.asyncio
async def test_toss_https_transport_allows_only_calendar_get_with_optional_date() -> (
    None
):
    transport_type = _transport_type(
        "autotrader.integrations.brokers.toss.transport", "TossHttpsTransport"
    )
    assert transport_type is not None
    opener = RecordingOpener()
    transport = transport_type(opener=opener)

    assert (
        await transport.request(
            BrokerRequest(
                method="GET", path="/api/v1/market-calendar/KR?date=2026-08-12"
            )
        )
    ).status == 200

    assert opener.requests[0].full_url == (
        "https://openapi.tossinvest.com/api/v1/market-calendar/KR?date=2026-08-12"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("POST", "/api/v1/market-calendar/KR"),
        ("GET", "/api/v1/market-calendar/KR?date=2026-08-12&extra=1"),
        ("GET", "/api/v1/market-calendar/KR/extra"),
        ("GET", "/api/v1/market-calendar/KR?route=write"),
    ),
)
async def test_toss_https_transport_blocks_noncanonical_calendar_routes_before_opening(
    method: str, path: str
) -> None:
    transport_type = _transport_type(
        "autotrader.integrations.brokers.toss.transport", "TossHttpsTransport"
    )
    assert transport_type is not None
    opener = RecordingOpener()
    transport = transport_type(opener=opener)

    with pytest.raises(BrokerWriteDisabled, match="not allowed"):
        await transport.request(BrokerRequest(method=method, path=path))

    assert opener.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("POST", "/api/v1/stocks/005930/warnings"),
        ("GET", "/api/v1/stocks/./warnings"),
        ("GET", "/api/v1/stocks/../warnings"),
        ("GET", "/api/v1/stocks/005930/extra/warnings"),
        ("GET", "/api/v1/stocks/005930/warnings/extra"),
        ("GET", "/api/v1/stocks/005930%2Fextra/warnings"),
        ("GET", "/api/v1/stocks/005930/warnings?write=1"),
        ("GET", "/api/v1/stocks/005930?route=/warnings"),
    ),
)
async def test_toss_https_transport_blocks_nonexact_warning_routes_before_opening(
    method: str, path: str
) -> None:
    transport_type = _transport_type(
        "autotrader.integrations.brokers.toss.transport", "TossHttpsTransport"
    )
    assert transport_type is not None
    opener = RecordingOpener()
    transport = transport_type(opener=opener)

    with pytest.raises(BrokerWriteDisabled, match="not allowed"):
        await transport.request(BrokerRequest(method=method, path=path))

    assert opener.requests == []


@pytest.mark.asyncio
async def test_toss_https_transport_blocks_order_creation_before_opening() -> None:
    transport_type = _transport_type(
        "autotrader.integrations.brokers.toss.transport", "TossHttpsTransport"
    )
    assert transport_type is not None
    opener = RecordingOpener()
    transport = transport_type(opener=opener)

    with pytest.raises(BrokerWriteDisabled, match="not allowed"):
        await transport.request(BrokerRequest(method="POST", path="/api/v1/orders"))

    assert opener.requests == []


@pytest.mark.asyncio
async def test_kis_https_transport_blocks_order_routes_before_opening() -> None:
    transport_type = _transport_type(
        "autotrader.integrations.brokers.kis.transport", "KisHttpsTransport"
    )
    assert transport_type is not None
    opener = RecordingOpener()
    transport = transport_type(opener=opener)

    with pytest.raises(BrokerWriteDisabled, match="not allowed"):
        await transport.request(BrokerRequest(method="POST", path="/uapi/orders"))

    with pytest.raises(BrokerWriteDisabled, match="not allowed"):
        await transport.request(
            BrokerRequest(
                method="POST",
                path="/uapi/overseas-futureoption/v1/trading/order",
            )
        )

    assert opener.requests == []


@pytest.mark.asyncio
async def test_kis_paper_https_transport_uses_only_vts_origin_and_blocks_orders() -> (
    None
):
    transport_type = _transport_type(
        "autotrader.integrations.brokers.kis.transport",
        "KisPaperHttpsTransport",
    )
    assert transport_type is not None
    opener = RecordingOpener()
    transport = transport_type(opener=opener)

    await transport.request(
        BrokerRequest(method="POST", path="/oauth2/tokenP", body=b"{}")
    )
    await transport.request(
        BrokerRequest(
            method="GET",
            path="/uapi/domestic-stock/v1/trading/inquire-balance?CANO=81012345",
        )
    )
    with pytest.raises(BrokerWriteDisabled, match="not allowed"):
        await transport.request(
            BrokerRequest(
                method="POST",
                path="/uapi/domestic-stock/v1/trading/order-cash",
            )
        )

    assert [request.full_url for request in opener.requests] == [
        "https://openapivts.koreainvestment.com:29443/oauth2/tokenP",
        "https://openapivts.koreainvestment.com:29443/"
        "uapi/domestic-stock/v1/trading/inquire-balance?CANO=81012345",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/uapi/domestic-stock/v1/quotations/inquire-index-category-price?FID_COND_MRKT_DIV_CODE=U",
        "/uapi/domestic-stock/v1/quotations/inquire-index-daily-price?FID_PERIOD_DIV_CODE=D",
    ],
)
async def test_kis_transport_allows_documented_index_reads(path: str) -> None:
    transport_type = _transport_type(
        "autotrader.integrations.brokers.kis.transport", "KisHttpsTransport"
    )
    assert transport_type is not None
    opener = RecordingOpener()
    transport = transport_type(opener=opener)

    assert (
        await transport.request(BrokerRequest(method="GET", path=path))
    ).status == 200
    assert [request.full_url for request in opener.requests] == [
        f"https://openapi.koreainvestment.com:9443{path}"
    ]


@pytest.mark.asyncio
async def test_kis_transport_allows_vi_read_but_blocks_cash_order_post() -> None:
    transport_type = _transport_type(
        "autotrader.integrations.brokers.kis.transport", "KisHttpsTransport"
    )
    assert transport_type is not None
    opener = RecordingOpener()
    transport = transport_type(opener=opener)
    path = "/uapi/domestic-stock/v1/quotations/inquire-vi-status?FID_INPUT_ISCD=005930"

    assert (
        await transport.request(BrokerRequest(method="GET", path=path))
    ).status == 200
    with pytest.raises(BrokerWriteDisabled, match="not allowed"):
        await transport.request(
            BrokerRequest(
                method="POST",
                path="/uapi/domestic-stock/v1/trading/order-cash",
            )
        )
    assert [request.full_url for request in opener.requests] == [
        f"https://openapi.koreainvestment.com:9443{path}"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/uapi/domestic-stock/v1/quotations/inquire-index-category-price?FID_COND_MRKT_DIV_CODE=U",
        "/uapi/domestic-stock/v1/quotations/inquire-index-daily-price?FID_PERIOD_DIV_CODE=D",
    ],
)
async def test_kis_transport_blocks_post_to_documented_index_reads_before_opening(
    path: str,
) -> None:
    transport_type = _transport_type(
        "autotrader.integrations.brokers.kis.transport", "KisHttpsTransport"
    )
    assert transport_type is not None
    opener = RecordingOpener()
    transport = transport_type(opener=opener)

    with pytest.raises(BrokerWriteDisabled, match="not allowed"):
        await transport.request(BrokerRequest(method="POST", path=path))

    assert opener.requests == []


@pytest.mark.asyncio
async def test_kis_https_transport_allows_contract_detail_reads() -> None:
    transport_type = _transport_type(
        "autotrader.integrations.brokers.kis.transport", "KisHttpsTransport"
    )
    assert transport_type is not None
    opener = RecordingOpener()
    transport = transport_type(opener=opener)

    await transport.request(
        BrokerRequest(
            method="GET",
            path=(
                "/uapi/overseas-futureoption/v1/quotations/"
                "search-contract-detail?QRY_CNT=1&SRS_CD_01=NQZ26"
            ),
        )
    )
    assert opener.requests[0].full_url == (
        "https://openapi.koreainvestment.com:9443/"
        "uapi/overseas-futureoption/v1/quotations/"
        "search-contract-detail?QRY_CNT=1&SRS_CD_01=NQZ26"
    )


@pytest.mark.asyncio
async def test_kis_https_transport_allows_domestic_quote_but_blocks_cash_order() -> (
    None
):
    transport_type = _transport_type(
        "autotrader.integrations.brokers.kis.transport", "KisHttpsTransport"
    )
    assert transport_type is not None
    opener = RecordingOpener()
    transport = transport_type(opener=opener)

    await transport.request(
        BrokerRequest(
            method="GET",
            path=(
                "/uapi/domestic-stock/v1/quotations/"
                "inquire-price?FID_COND_MRKT_DIV_CODE=J&FID_INPUT_ISCD=005930"
            ),
        )
    )
    await transport.request(
        BrokerRequest(
            method="GET",
            path="/uapi/domestic-stock/v1/trading/inquire-balance?CANO=81012345",
        )
    )
    await transport.request(
        BrokerRequest(
            method="GET",
            path="/uapi/domestic-stock/v1/trading/inquire-psbl-order?CANO=81012345",
        )
    )
    await transport.request(
        BrokerRequest(
            method="GET",
            path=(
                "/uapi/domestic-stock/v1/quotations/"
                "inquire-time-dailychartprice?FID_COND_MRKT_DIV_CODE=J&"
                "FID_INPUT_ISCD=005930"
            ),
        )
    )
    await transport.request(
        BrokerRequest(
            method="GET",
            path=(
                "/uapi/domestic-stock/v1/quotations/"
                "inquire-daily-itemchartprice?FID_COND_MRKT_DIV_CODE=J&"
                "FID_INPUT_ISCD=005930"
            ),
        )
    )
    with pytest.raises(BrokerWriteDisabled, match="not allowed"):
        await transport.request(
            BrokerRequest(
                method="POST",
                path="/uapi/domestic-stock/v1/trading/order-cash",
            )
        )

    assert [request.full_url for request in opener.requests] == [
        "https://openapi.koreainvestment.com:9443/"
        "uapi/domestic-stock/v1/quotations/"
        "inquire-price?FID_COND_MRKT_DIV_CODE=J&FID_INPUT_ISCD=005930",
        "https://openapi.koreainvestment.com:9443/"
        "uapi/domestic-stock/v1/trading/inquire-balance?CANO=81012345",
        "https://openapi.koreainvestment.com:9443/"
        "uapi/domestic-stock/v1/trading/inquire-psbl-order?CANO=81012345",
        "https://openapi.koreainvestment.com:9443/"
        "uapi/domestic-stock/v1/quotations/"
        "inquire-time-dailychartprice?FID_COND_MRKT_DIV_CODE=J&"
        "FID_INPUT_ISCD=005930",
        "https://openapi.koreainvestment.com:9443/"
        "uapi/domestic-stock/v1/quotations/"
        "inquire-daily-itemchartprice?FID_COND_MRKT_DIV_CODE=J&"
        "FID_INPUT_ISCD=005930",
    ]
