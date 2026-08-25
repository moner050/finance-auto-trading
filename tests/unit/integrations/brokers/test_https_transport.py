from __future__ import annotations

import gzip
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.message import Message
from io import BytesIO
from typing import Never
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from autotrader.integrations.brokers.common import (
    BrokerRequest,
    BrokerTransportError,
    BrokerWriteDisabled,
    WhitelistedHttpsTransport,
)


@dataclass
class FakeHttpResponse:
    status: int = 200
    body: bytes = b'{"ok":true}'
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


@dataclass
class HttpErrorOpener:
    body: bytes = b'{"error":"invalid_token"}'

    def __call__(self, request: Request, timeout: float) -> Never:
        del timeout
        raise HTTPError(
            url=request.full_url,
            code=401,
            msg="Unauthorized",
            hdrs=Message(),
            fp=BytesIO(self.body),
        )


@pytest.mark.asyncio
async def test_whitelisted_https_transport_sends_only_declared_read_route() -> None:
    opener = RecordingOpener()
    transport = WhitelistedHttpsTransport(
        base_url="https://broker.example",
        allowed_routes=frozenset({("GET", "/quotes")}),
        opener=opener,
    )

    response = await transport.request(BrokerRequest(method="GET", path="/quotes?x=1"))

    assert response.status == 200
    assert response.body == b'{"ok":true}'
    assert response.headers == ()
    assert len(opener.requests) == 1
    assert opener.requests[0].full_url == "https://broker.example/quotes?x=1"


@pytest.mark.asyncio
async def test_whitelisted_https_transport_blocks_undeclared_post_before_opening() -> (
    None
):
    opener = RecordingOpener()
    transport = WhitelistedHttpsTransport(
        base_url="https://broker.example",
        allowed_routes=frozenset({("GET", "/quotes")}),
        opener=opener,
    )

    with pytest.raises(BrokerWriteDisabled, match="not allowed"):
        await transport.request(BrokerRequest(method="POST", path="/orders"))

    assert opener.requests == []


@pytest.mark.asyncio
async def test_whitelisted_https_transport_sends_only_declared_delete_route() -> None:
    opener = RecordingOpener()
    transport = WhitelistedHttpsTransport(
        base_url="https://broker.example",
        allowed_routes=frozenset({("DELETE", "/fapi/v1/order")}),
        opener=opener,
    )

    response = await transport.request(
        BrokerRequest(method="DELETE", path="/fapi/v1/order?symbol=BTCUSDT")
    )

    assert response.status == 200
    assert len(opener.requests) == 1
    assert opener.requests[0].method == "DELETE"
    assert (
        opener.requests[0].full_url
        == "https://broker.example/fapi/v1/order?symbol=BTCUSDT"
    )


def test_whitelisted_https_transport_keeps_put_route_unsupported() -> None:
    with pytest.raises(ValueError, match="GET, POST, or DELETE"):
        WhitelistedHttpsTransport(
            base_url="https://broker.example",
            allowed_routes=frozenset({("PUT", "/fapi/v1/order")}),
        )


@pytest.mark.asyncio
async def test_whitelisted_https_transport_preserves_provider_http_errors() -> None:
    transport = WhitelistedHttpsTransport(
        base_url="https://broker.example",
        allowed_routes=frozenset({("GET", "/quotes")}),
        opener=HttpErrorOpener(),
    )

    response = await transport.request(BrokerRequest(method="GET", path="/quotes"))

    assert response.status == 401
    assert response.body == b'{"error":"invalid_token"}'


@pytest.mark.asyncio
async def test_whitelisted_https_transport_bounds_wire_response_read() -> None:
    response = FakeHttpResponse(body=b"12345")
    opener = RecordingOpener(response=response)
    transport = WhitelistedHttpsTransport(
        base_url="https://broker.example",
        allowed_routes=frozenset({("GET", "/quotes")}),
        opener=opener,
        max_response_bytes=4,
    )

    with pytest.raises(BrokerTransportError, match="size"):
        await transport.request(BrokerRequest(method="GET", path="/quotes"))

    assert response.read_amounts == [5]


@pytest.mark.asyncio
async def test_whitelisted_https_transport_bounds_decoded_gzip_response() -> None:
    response = FakeHttpResponse(
        body=gzip.compress(b"x" * 256),
        headers={"Content-Encoding": "gzip"},
    )
    opener = RecordingOpener(response=response)
    transport = WhitelistedHttpsTransport(
        base_url="https://broker.example",
        allowed_routes=frozenset({("GET", "/quotes")}),
        opener=opener,
        max_response_bytes=128,
    )

    with pytest.raises(BrokerTransportError, match="size"):
        await transport.request(BrokerRequest(method="GET", path="/quotes"))

    assert response.read_amounts == [129]


@pytest.mark.asyncio
async def test_whitelisted_https_transport_bounds_http_error_response() -> None:
    transport = WhitelistedHttpsTransport(
        base_url="https://broker.example",
        allowed_routes=frozenset({("GET", "/quotes")}),
        opener=HttpErrorOpener(body=b"12345"),
        max_response_bytes=4,
    )

    with pytest.raises(BrokerTransportError, match="size"):
        await transport.request(BrokerRequest(method="GET", path="/quotes"))
