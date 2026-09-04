from __future__ import annotations

import asyncio
import gzip
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from autotrader.domain.broker_errors import (
    BrokerSubmissionRejected as BrokerSubmissionRejected,
)
from autotrader.domain.enums import BrokerProvider as BrokerProvider


class BrokerMarket(StrEnum):
    KRX_STOCK = "KRX_STOCK"
    US_STOCK = "US_STOCK"
    OVERSEAS_FUTURES = "OVERSEAS_FUTURES"
    BINANCE_USDM = "BINANCE_USDM"


class BrokerCapability(StrEnum):
    MARKET_DATA = "MARKET_DATA"
    ACCOUNT_READ = "ACCOUNT_READ"
    STOCK_ORDER = "STOCK_ORDER"
    OVERSEAS_FUTURES = "OVERSEAS_FUTURES"
    USD_M_FUTURES = "USD_M_FUTURES"


class BrokerWriteDisabled(RuntimeError):
    """Raised before a disabled broker write can reach a transport."""


class BrokerTransportError(RuntimeError):
    """Raised when an approved HTTPS broker request cannot reach its provider."""


class UnsupportedBrokerMarket(ValueError):
    """Raised when a provider cannot serve the requested market."""


class UnsupportedBrokerInstrument(ValueError):
    """Raised when a verified provider instrument mapping is absent."""


@dataclass(frozen=True, slots=True)
class BrokerRequest:
    method: str
    path: str
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None

    def __post_init__(self) -> None:
        method = self.method.upper()
        if method not in {"GET", "POST", "DELETE"}:
            raise ValueError("broker request method must be GET, POST, or DELETE")
        if not self.path.startswith("/") or "//" in self.path:
            raise ValueError("broker request path must be relative")
        normalized_headers = tuple(
            sorted(self.headers, key=lambda item: item[0].lower())
        )
        if any(
            not key or not value or "\n" in key or "\n" in value
            for key, value in normalized_headers
        ):
            raise ValueError("broker request headers must be non-empty single lines")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "headers", normalized_headers)


@dataclass(frozen=True, slots=True)
class BrokerResponse:
    status: int
    body: bytes
    headers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not 100 <= self.status <= 599:
            raise ValueError("broker response requires an HTTP status")
        normalized_headers = tuple(
            sorted(self.headers, key=lambda item: item[0].lower())
        )
        if any(
            not key or "\n" in key or "\n" in value for key, value in normalized_headers
        ):
            raise ValueError("broker response headers must be non-empty single lines")
        object.__setattr__(self, "headers", normalized_headers)

    def header(self, name: str) -> str | None:
        return next(
            (value for key, value in self.headers if key.lower() == name.lower()),
            None,
        )


class AsyncHttpTransport(Protocol):
    async def request(self, request: BrokerRequest) -> BrokerResponse: ...


class _HttpResponse(Protocol):
    status: int
    headers: Mapping[str, str]

    def __enter__(self) -> _HttpResponse: ...

    def __exit__(self, *args: object) -> None: ...

    def read(self, amount: int = -1) -> bytes: ...


HttpOpener = Callable[[Request, float], _HttpResponse]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _open_https(request: Request, timeout_seconds: float) -> _HttpResponse:
    return build_opener(_NoRedirect()).open(request, timeout=timeout_seconds)


class WhitelistedHttpsTransport:
    """HTTPS transport that can reach only immutable, exactly allowed routes."""

    def __init__(
        self,
        *,
        base_url: str,
        allowed_routes: frozenset[tuple[str, str]],
        opener: HttpOpener | None = None,
        timeout_seconds: float = 10.0,
        max_response_bytes: int | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be an HTTPS origin")
        if not allowed_routes or any(
            method not in {"GET", "POST", "DELETE"}
            or not path.startswith("/")
            or "?" in path
            or "#" in path
            for method, path in allowed_routes
        ):
            raise ValueError(
                "allowed_routes must contain absolute GET, POST, or DELETE paths"
            )
        if type(timeout_seconds) not in {int, float} or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_response_bytes is not None and (
            type(max_response_bytes) is not int or max_response_bytes <= 0
        ):
            raise ValueError("max_response_bytes must be a positive integer")
        self._base_url = base_url.rstrip("/")
        self._allowed_routes = allowed_routes
        self._opener = _open_https if opener is None else opener
        self._timeout_seconds = float(timeout_seconds)
        self._max_response_bytes = max_response_bytes

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        if not self._route_is_allowed(request):
            raise BrokerWriteDisabled("broker transport route is not allowed")
        return await asyncio.to_thread(self._request_sync, request)

    def _route_is_allowed(self, request: BrokerRequest) -> bool:
        path, _, _ = request.path.partition("?")
        return (request.method, path) in self._allowed_routes

    def _request_sync(self, request: BrokerRequest) -> BrokerResponse:
        http_request = Request(
            url=f"{self._base_url}{request.path}",
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            with self._opener(http_request, self._timeout_seconds) as response:
                headers = tuple(response.headers.items())
                return BrokerResponse(
                    status=response.status,
                    body=_decode_response_body(
                        _read_response_body(response, self._max_response_bytes),
                        headers=headers,
                        max_decoded_bytes=self._max_response_bytes,
                    ),
                    headers=headers,
                )
        except HTTPError as error:
            try:
                response_headers = getattr(error, "headers", None)
                headers = (
                    () if response_headers is None else tuple(response_headers.items())
                )
                return BrokerResponse(
                    status=error.code,
                    body=_decode_response_body(
                        _read_response_body(error, self._max_response_bytes),
                        headers=headers,
                        max_decoded_bytes=self._max_response_bytes,
                    ),
                    headers=headers,
                )
            finally:
                error.close()
        except (OSError, URLError) as error:
            raise BrokerTransportError("broker HTTPS request failed") from error


def _read_response_body(
    response: _HttpResponse | HTTPError,
    max_response_bytes: int | None,
) -> bytes:
    if max_response_bytes is None:
        return response.read()
    body = response.read(max_response_bytes + 1)
    if len(body) > max_response_bytes:
        raise BrokerTransportError("broker HTTPS response size is invalid")
    return body


def _decode_response_body(
    body: bytes,
    *,
    headers: tuple[tuple[str, str], ...],
    max_decoded_bytes: int | None = None,
) -> bytes:
    content_encoding = (
        next(
            (value for name, value in headers if name.lower() == "content-encoding"),
            "",
        )
        .strip()
        .lower()
    )
    if content_encoding in {"", "identity"}:
        if max_decoded_bytes is not None and len(body) > max_decoded_bytes:
            raise BrokerTransportError("broker HTTPS response size is invalid")
        return body
    if content_encoding != "gzip":
        raise BrokerTransportError("broker HTTPS response encoding is unsupported")
    try:
        if max_decoded_bytes is None:
            return gzip.decompress(body)
        with gzip.GzipFile(fileobj=BytesIO(body)) as compressed:
            decoded = compressed.read(max_decoded_bytes + 1)
        if len(decoded) > max_decoded_bytes:
            raise BrokerTransportError("broker HTTPS response size is invalid")
        return decoded
    except (gzip.BadGzipFile, EOFError, OSError, zlib.error) as error:
        raise BrokerTransportError("broker HTTPS response body is invalid") from error


# What a command must carry before any adapter will write it to a venue.
#
# Every adapter used to check `origin_type == "DAVID_V6_DECISION"` and
# `authority_class == "V6_PROVIDER_WRITE"`. Nothing mints either string: the
# loop emits `IntentOrigin` values and the two SUBMIT authorities below, so
# those checks refused every order that reached them - in three adapters, none
# of which had been wired to a venue, so none of them ever said so.
#
# The authority is deliberately not collapsed to one value. `OrderCommandFactory`
# uses it to keep a closing order from borrowing the authority that opens
# exposure, and a single constant for both would erase that distinction at the
# only boundary that could still check it.
PROVIDER_WRITE_ORIGINS = frozenset({"STRATEGY", "PROTECTION"})
OPENING_AUTHORITY = "SUBMIT_NEW_EXPOSURE"
CLOSING_AUTHORITY = "SUBMIT_STRICT_REDUCTION"
PROVIDER_WRITE_AUTHORITIES = frozenset({OPENING_AUTHORITY, CLOSING_AUTHORITY})


def writes_to_a_venue(origin_type: str, authority_class: str) -> bool:
    """Whether this command is one the loop authorised a venue write for.

    An operator or reconciliation origin is refused here rather than
    accepted quietly: those exist, but nothing in this strategy's loop sends
    them to a venue, and an adapter that took them would execute something no
    strategy decision produced.
    """
    return (
        origin_type in PROVIDER_WRITE_ORIGINS
        and authority_class in PROVIDER_WRITE_AUTHORITIES
    )
