from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qsl

import pytest
from pydantic import SecretStr

from autotrader.integrations.brokers.binance_usdm.rate_limit import (
    BinanceUsdmRateLimiter,
)
from autotrader.integrations.brokers.binance_usdm.secrets import BinanceUsdmSecret
from autotrader.integrations.brokers.binance_usdm.transport import (
    BinanceUsdmAmbiguousWrite,
    BinanceUsdmClockError,
    BinanceUsdmTransport,
)
from autotrader.integrations.brokers.common import BrokerRequest, BrokerResponse


@dataclass
class _Clock:
    milliseconds: int = 1_800_000_000_000
    monotonic_seconds: float = 0.0
    sleeps: list[float] = field(default_factory=list[float])

    def now_ms(self) -> int:
        return self.milliseconds

    def monotonic(self) -> float:
        return self.monotonic_seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.monotonic_seconds += seconds
        self.milliseconds += round(seconds * 1000)


class _RawTransport:
    def __init__(self, responses: list[BrokerResponse]) -> None:
        self.responses = responses
        self.requests: list[BrokerRequest] = []

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def _secret() -> BinanceUsdmSecret:
    return BinanceUsdmSecret(
        api_key=SecretStr("private-api-key"),
        secret_key=SecretStr("private-secret-key"),
    )


def _transport(
    raw: _RawTransport,
    clock: _Clock,
    *,
    limiter: BinanceUsdmRateLimiter | None = None,
) -> BinanceUsdmTransport:
    shared = limiter or BinanceUsdmRateLimiter(
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    return BinanceUsdmTransport(
        transport=raw,
        secret=_secret(),
        now_ms=clock.now_ms,
        rate_limiter=shared,
        recv_window_ms=5000,
        maximum_clock_offset_ms=5000,
        maximum_sync_round_trip_ms=1000,
        clock_sync_ttl_ms=30_000,
    )


@pytest.mark.asyncio
async def test_lazily_syncs_server_clock_and_signs_a_post_body() -> None:
    clock = _Clock()
    server_time = clock.milliseconds + 250
    raw = _RawTransport(
        [
            BrokerResponse(200, f'{{"serverTime":{server_time}}}'.encode()),
            BrokerResponse(
                200,
                b'{"orderId":1}',
                headers=(
                    ("X-MBX-USED-WEIGHT-1M", "2"),
                    ("X-MBX-ORDER-COUNT-10S", "1"),
                ),
            ),
        ]
    )
    transport = _transport(raw, clock)

    response = await transport.send(
        BrokerRequest(
            method="POST",
            path="/fapi/v1/order",
            body=b"symbol=BTCUSDT&side=BUY&type=MARKET&quantity=0.001",
        )
    )

    assert response.status == 200
    assert raw.requests[0] == BrokerRequest(method="GET", path="/fapi/v1/time")
    signed = raw.requests[1]
    assert signed.path == "/fapi/v1/order"
    assert dict(signed.headers) == {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-MBX-APIKEY": "private-api-key",
    }
    assert signed.body is not None
    parameters = dict(parse_qsl(signed.body.decode(), strict_parsing=True))
    assert parameters | {"signature": parameters["signature"]} == parameters
    assert parameters["symbol"] == "BTCUSDT"
    assert parameters["recvWindow"] == "5000"
    assert parameters["timestamp"] == str(server_time)
    assert len(parameters["signature"]) == 64


@pytest.mark.asyncio
async def test_reuses_fresh_clock_offset_and_signs_get_query_parameters() -> None:
    clock = _Clock()
    raw = _RawTransport(
        [
            BrokerResponse(200, f'{{"serverTime":{clock.milliseconds}}}'.encode()),
            BrokerResponse(200, b"{}"),
            BrokerResponse(200, b"{}"),
        ]
    )
    transport = _transport(raw, clock)

    await transport.send(
        BrokerRequest(method="GET", path="/fapi/v1/order?symbol=BTCUSDT&orderId=1")
    )
    await transport.send(
        BrokerRequest(
            method="GET",
            path="/fapi/v1/algoOrder?clientAlgoId=protection-1",
        )
    )

    assert len(raw.requests) == 3
    first_query = dict(
        parse_qsl(raw.requests[1].path.partition("?")[2], strict_parsing=True)
    )
    second_query = dict(
        parse_qsl(raw.requests[2].path.partition("?")[2], strict_parsing=True)
    )
    assert first_query["recvWindow"] == "5000"
    assert second_query["clientAlgoId"] == "protection-1"
    assert len(first_query["signature"]) == len(second_query["signature"]) == 64


@pytest.mark.asyncio
async def test_existing_timing_signature_or_credentials_fail_before_network() -> None:
    clock = _Clock()
    raw = _RawTransport([])
    transport = _transport(raw, clock)

    invalid = (
        BrokerRequest(method="GET", path="/fapi/v1/order?timestamp=1"),
        BrokerRequest(method="GET", path="/fapi/v1/order?signature=abc"),
        BrokerRequest(method="POST", path="/fapi/v1/order", body=b"recvWindow=1"),
        BrokerRequest(
            method="GET",
            path="/fapi/v1/order",
            headers=(("X-MBX-APIKEY", "caller-key"),),
        ),
    )
    for request in invalid:
        with pytest.raises(ValueError, match="request"):
            await transport.send(request)

    assert raw.requests == []


@pytest.mark.asyncio
async def test_rejects_invalid_or_excessively_drifted_server_time() -> None:
    clock = _Clock()
    invalid = _transport(
        _RawTransport([BrokerResponse(200, b'{"serverTime":"not-an-int"}')]),
        clock,
    )
    drifted = _transport(
        _RawTransport(
            [
                BrokerResponse(
                    200,
                    f'{{"serverTime":{clock.milliseconds + 5001}}}'.encode(),
                )
            ]
        ),
        clock,
    )

    with pytest.raises(BinanceUsdmClockError):
        await invalid.send(BrokerRequest(method="GET", path="/fapi/v3/balance"))
    with pytest.raises(BinanceUsdmClockError):
        await drifted.send(BrokerRequest(method="GET", path="/fapi/v3/balance"))


@pytest.mark.asyncio
async def test_http_5xx_write_is_ambiguous_without_response_body_leak() -> None:
    clock = _Clock()
    raw = _RawTransport(
        [
            BrokerResponse(200, f'{{"serverTime":{clock.milliseconds}}}'.encode()),
            BrokerResponse(503, b"private raw provider body and signed query"),
        ]
    )
    transport = _transport(raw, clock)

    with pytest.raises(BinanceUsdmAmbiguousWrite) as raised:
        await transport.send(
            BrokerRequest(
                method="POST",
                path="/fapi/v1/order",
                body=b"symbol=BTCUSDT&side=BUY&type=MARKET&quantity=0.001",
            )
        )

    assert raised.value.status == 503
    assert "private" not in repr(raised.value)
    assert "signature" not in repr(raised.value)


@pytest.mark.asyncio
async def test_public_reads_are_not_signed_and_read_5xx_is_returned() -> None:
    clock = _Clock()
    raw = _RawTransport([BrokerResponse(500, b"unavailable")])
    transport = _transport(raw, clock)

    response = await transport.send(
        BrokerRequest(method="GET", path="/fapi/v1/exchangeInfo")
    )

    assert response.status == 500
    assert raw.requests == [BrokerRequest(method="GET", path="/fapi/v1/exchangeInfo")]
