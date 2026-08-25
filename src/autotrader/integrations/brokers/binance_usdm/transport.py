from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import cast
from urllib.parse import parse_qsl

from autotrader.integrations.brokers.binance_usdm.rate_limit import (
    BinanceUsdmRateLimiter,
)
from autotrader.integrations.brokers.binance_usdm.secrets import BinanceUsdmSecret
from autotrader.integrations.brokers.binance_usdm.signing import (
    encode_query,
    sign_query,
)
from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerRequest,
    BrokerResponse,
)

_PUBLIC_ROUTES = frozenset(
    {
        ("GET", "/fapi/v1/time"),
        ("GET", "/fapi/v1/exchangeInfo"),
    }
)
_RESERVED_PARAMETERS = frozenset({"recvWindow", "timestamp", "signature"})
_SENSITIVE_HEADERS = frozenset({"x-mbx-apikey", "authorization"})


class BinanceUsdmClockError(RuntimeError):
    """Raised when a safe signed timestamp cannot be established."""


class BinanceUsdmAmbiguousWrite(RuntimeError):
    """A write may have reached Binance but its terminal outcome is unknown."""

    def __init__(self, status: int) -> None:
        super().__init__("Binance USD-M write outcome is ambiguous")
        self.status = status


class BinanceUsdmTransport:
    def __init__(
        self,
        *,
        transport: AsyncHttpTransport,
        secret: BinanceUsdmSecret,
        now_ms: Callable[[], int],
        rate_limiter: BinanceUsdmRateLimiter,
        recv_window_ms: int = 5000,
        maximum_clock_offset_ms: int = 5000,
        maximum_sync_round_trip_ms: int = 1000,
        clock_sync_ttl_ms: int = 30_000,
    ) -> None:
        if type(secret) is not BinanceUsdmSecret:
            raise TypeError("Binance USD-M transport secret must be exact")
        if (
            type(recv_window_ms) is not int
            or not 1 <= recv_window_ms <= 60_000
            or type(maximum_clock_offset_ms) is not int
            or maximum_clock_offset_ms <= 0
            or type(maximum_sync_round_trip_ms) is not int
            or maximum_sync_round_trip_ms <= 0
            or type(clock_sync_ttl_ms) is not int
            or clock_sync_ttl_ms <= 0
            or not callable(now_ms)
        ):
            raise ValueError("Binance USD-M transport configuration is invalid")
        self._transport = transport
        self._secret = secret
        self._now_ms = now_ms
        self._rate_limiter = rate_limiter
        self._recv_window_ms = recv_window_ms
        self._maximum_clock_offset_ms = maximum_clock_offset_ms
        self._maximum_sync_round_trip_ms = maximum_sync_round_trip_ms
        self._clock_sync_ttl_ms = clock_sync_ttl_ms
        self._clock_offset_ms: int | None = None
        self._clock_synced_local_ms: int | None = None
        self._clock_lock = asyncio.Lock()

    async def send(self, request: BrokerRequest) -> BrokerResponse:
        if type(request) is not BrokerRequest:
            raise TypeError("Binance USD-M request must be exact")
        path, parameters = _request_parameters(request)
        _validate_headers(request)
        if (request.method, path) in _PUBLIC_ROUTES:
            if parameters or request.body is not None or request.headers:
                raise ValueError("Binance USD-M public request is invalid")
            return await self._send_raw(request)
        if not path.startswith("/fapi/"):
            raise ValueError("Binance USD-M signed request is invalid")
        if any(key in _RESERVED_PARAMETERS for key, _ in parameters):
            raise ValueError("Binance USD-M signed request is invalid")
        await self._ensure_clock()
        timestamp = self._timestamp()
        signed = self._signed_request(
            request,
            path=path,
            parameters=(
                *parameters,
                ("recvWindow", str(self._recv_window_ms)),
                ("timestamp", str(timestamp)),
            ),
        )
        response = await self._send_raw(signed)
        if request.method == "POST" and (
            response.status == 408 or response.status >= 500
        ):
            raise BinanceUsdmAmbiguousWrite(response.status)
        return response

    async def _ensure_clock(self) -> None:
        now = _clock_ms(self._now_ms)
        synced_at = self._clock_synced_local_ms
        if synced_at is not None:
            age = now - synced_at
            if age < 0:
                raise BinanceUsdmClockError("Binance USD-M local clock regressed")
            if age <= self._clock_sync_ttl_ms:
                return
        async with self._clock_lock:
            now = _clock_ms(self._now_ms)
            synced_at = self._clock_synced_local_ms
            if (
                synced_at is not None
                and 0 <= now - synced_at <= self._clock_sync_ttl_ms
            ):
                return
            local_before = now
            response = await self._send_raw(
                BrokerRequest(method="GET", path="/fapi/v1/time")
            )
            local_after = _clock_ms(self._now_ms)
            round_trip = local_after - local_before
            if (
                response.status != 200
                or round_trip < 0
                or round_trip > self._maximum_sync_round_trip_ms
            ):
                raise BinanceUsdmClockError("Binance USD-M server clock is unavailable")
            server_time = _server_time(response)
            midpoint = local_before + round_trip // 2
            offset = server_time - midpoint
            if abs(offset) > self._maximum_clock_offset_ms:
                raise BinanceUsdmClockError(
                    "Binance USD-M server clock drift is unsafe"
                )
            self._clock_offset_ms = offset
            self._clock_synced_local_ms = local_after

    def _timestamp(self) -> int:
        if self._clock_offset_ms is None:
            raise BinanceUsdmClockError("Binance USD-M server clock is unavailable")
        return _clock_ms(self._now_ms) + self._clock_offset_ms

    def _signed_request(
        self,
        request: BrokerRequest,
        *,
        path: str,
        parameters: tuple[tuple[str, str], ...],
    ) -> BrokerRequest:
        signature = sign_query(parameters, self._secret.secret_key)
        encoded = encode_query((*parameters, ("signature", signature)))
        api_key = self._secret.api_key.get_secret_value()
        if request.method == "POST":
            result = BrokerRequest(
                method="POST",
                path=path,
                headers=(
                    ("Content-Type", "application/x-www-form-urlencoded"),
                    ("X-MBX-APIKEY", api_key),
                ),
                body=encoded.encode("ascii"),
            )
        else:
            result = BrokerRequest(
                method=request.method,
                path=f"{path}?{encoded}",
                headers=(("X-MBX-APIKEY", api_key),),
            )
        del api_key
        return result

    async def _send_raw(self, request: BrokerRequest) -> BrokerResponse:
        await self._rate_limiter.before_request()
        response = await self._transport.request(request)
        await self._rate_limiter.observe_response(response)
        return response


def _request_parameters(
    request: BrokerRequest,
) -> tuple[str, tuple[tuple[str, str], ...]]:
    if "#" in request.path or request.path.count("?") > 1:
        raise ValueError("Binance USD-M request is invalid")
    path, separator, query = request.path.partition("?")
    if request.method == "POST":
        if separator:
            raise ValueError("Binance USD-M POST request is invalid")
        raw = b"" if request.body is None else request.body
        try:
            encoded = raw.decode("ascii")
        except UnicodeDecodeError as error:
            raise ValueError("Binance USD-M POST request is invalid") from error
    else:
        if request.body is not None:
            raise ValueError("Binance USD-M request body is invalid")
        encoded = query if separator else ""
    if not encoded:
        return path, ()
    try:
        parameters = tuple(
            parse_qsl(encoded, keep_blank_values=True, strict_parsing=True)
        )
    except ValueError as error:
        raise ValueError("Binance USD-M request parameters are invalid") from error
    if not parameters or len({key for key, _ in parameters}) != len(parameters):
        raise ValueError("Binance USD-M request parameters are invalid")
    return path, parameters


def _validate_headers(request: BrokerRequest) -> None:
    normalized: set[str] = set()
    for name, value in request.headers:
        key = name.casefold()
        if key in normalized or key in _SENSITIVE_HEADERS:
            raise ValueError("Binance USD-M request headers are invalid")
        normalized.add(key)
        if key != "content-type" or value != "application/x-www-form-urlencoded":
            raise ValueError("Binance USD-M request headers are invalid")
    if request.method != "POST" and normalized:
        raise ValueError("Binance USD-M request headers are invalid")


def _server_time(response: BrokerResponse) -> int:
    try:
        payload = cast(object, json.loads(response.body))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BinanceUsdmClockError(
            "Binance USD-M server clock is unavailable"
        ) from error
    if not isinstance(payload, dict):
        raise BinanceUsdmClockError("Binance USD-M server clock is unavailable")
    value = cast(dict[object, object], payload).get("serverTime")
    if type(value) is not int or value < 0:
        raise BinanceUsdmClockError("Binance USD-M server clock is unavailable")
    return value


def _clock_ms(now_ms: Callable[[], int]) -> int:
    value = now_ms()
    if type(value) is not int or value < 0:
        raise BinanceUsdmClockError("Binance USD-M local clock is invalid")
    return value


__all__ = (
    "BinanceUsdmAmbiguousWrite",
    "BinanceUsdmClockError",
    "BinanceUsdmTransport",
)
