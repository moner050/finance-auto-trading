from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from enum import StrEnum
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerRequest,
    BrokerResponse,
)


class TossRateGroup(StrEnum):
    AUTH = "AUTH"
    ACCOUNT = "ACCOUNT"
    ASSET = "ASSET"
    ORDER = "ORDER"
    ORDER_INFO = "ORDER_INFO"
    ORDER_HISTORY = "ORDER_HISTORY"
    MARKET_INFO = "MARKET_INFO"


class TossRateLimitUnavailable(RuntimeError):
    """Raised when a read cannot remain inside the pinned Toss rate contract."""


@dataclass(slots=True)
class _Bucket:
    documented_limit: int
    reported_limit: int
    tokens: float
    refill_seconds: float
    updated_at: float


_KST = ZoneInfo("Asia/Seoul")
_PEAK_START = time(9, 0)
_PEAK_END = time(9, 10)
_NUMBER = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_DOCUMENTED_LIMITS = {
    TossRateGroup.AUTH: 5,
    TossRateGroup.ACCOUNT: 1,
    TossRateGroup.ASSET: 5,
    TossRateGroup.ORDER: 10,
    TossRateGroup.ORDER_INFO: 6,
    TossRateGroup.ORDER_HISTORY: 5,
    TossRateGroup.MARKET_INFO: 3,
}
_RATE_HEADERS = (
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
)


class TossRateLimitedTransport:
    """Read-only Toss transport wrapper with independent provider-rate buckets."""

    def __init__(
        self,
        *,
        transport: AsyncHttpTransport,
        monotonic: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        wall_clock: Callable[[], datetime] | None = None,
        jitter: Callable[[int], float],
        deadline: float,
    ) -> None:
        start = monotonic()
        if (
            not math.isfinite(start)
            or not math.isfinite(deadline)
            or deadline <= start
            or not callable(sleep)
            or not callable(jitter)
        ):
            raise ValueError("Toss rate limiter configuration is invalid")
        self._transport = transport
        self._monotonic = monotonic
        self._sleep = sleep
        self._wall_clock = _utc_now if wall_clock is None else wall_clock
        self._jitter = jitter
        self._deadline = deadline
        self._buckets: dict[TossRateGroup, _Bucket] = {}
        self._locks = {group: asyncio.Lock() for group in TossRateGroup}

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        outcome = await self._request_outcome(request)
        del self, request
        if isinstance(outcome, BaseException):
            error = outcome
            outcome = None
            _scrub_control(error)
            raise error from None
        if outcome is None:
            raise TossRateLimitUnavailable("Toss rate-limited read is unavailable")
        return outcome

    async def _request_outcome(
        self, request: BrokerRequest
    ) -> BrokerResponse | BaseException | None:
        try:
            group = _request_group(request)
            async with self._locks[group]:
                return await self._request_in_group(group, request)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as caught:
            _scrub_control(caught)
            return caught
        except Exception as caught:
            _scrub_exception(caught)
            return None

    async def _request_in_group(
        self, group: TossRateGroup, request: BrokerRequest
    ) -> BrokerResponse:
        bucket = self._bucket(group)
        await self._consume_token(bucket)
        response = await self._transport.request(request)
        retry = 0
        while True:
            reset = self._update_bucket(bucket, response)
            if response.status != 429:
                return response
            if retry >= 1:
                raise ValueError("Toss rate limit retry is exhausted")
            retry_after = _single_numeric_header(response, "Retry-After")
            jitter = self._jitter(retry)
            if (
                type(jitter) not in {int, float}
                or not math.isfinite(jitter)
                or jitter < 0
                or jitter > 0.1
            ):
                raise ValueError("Toss rate limit jitter is invalid")
            delay = max(retry_after, reset, 1.0) + float(jitter)
            await self._sleep_before_deadline(delay)
            await self._consume_token(bucket)
            response = await self._transport.request(request)
            retry += 1

    def _bucket(self, group: TossRateGroup) -> _Bucket:
        documented_limit = _documented_limit(group, self._wall_clock())
        bucket = self._buckets.get(group)
        if bucket is None:
            bucket = _Bucket(
                documented_limit=documented_limit,
                reported_limit=documented_limit,
                tokens=float(documented_limit),
                refill_seconds=1 / documented_limit,
                updated_at=self._monotonic(),
            )
            self._buckets[group] = bucket
        else:
            bucket.documented_limit = documented_limit
            bucket.tokens = min(bucket.tokens, float(_capacity(bucket)))
        return bucket

    async def _consume_token(self, bucket: _Bucket) -> None:
        now = self._monotonic()
        if not math.isfinite(now):
            raise ValueError("Toss rate limit clock is invalid")
        elapsed = max(0.0, now - bucket.updated_at)
        capacity = _capacity(bucket)
        bucket.tokens = min(
            float(capacity), bucket.tokens + elapsed / bucket.refill_seconds
        )
        bucket.updated_at = now
        if bucket.tokens < 1:
            delay = (1 - bucket.tokens) * bucket.refill_seconds
            await self._sleep_before_deadline(delay)
            now = self._monotonic()
            bucket.tokens = min(
                float(capacity),
                bucket.tokens
                + max(0.0, now - bucket.updated_at) / bucket.refill_seconds,
            )
            bucket.updated_at = now
        if bucket.tokens < 1 - 1e-9:
            raise ValueError("Toss rate limit token did not refill")
        bucket.tokens = max(0.0, bucket.tokens - 1)

    def _update_bucket(self, bucket: _Bucket, response: BrokerResponse) -> float:
        limit = _single_integer_header(response, _RATE_HEADERS[0])
        remaining = _single_integer_header(response, _RATE_HEADERS[1])
        reset = _single_numeric_header(response, _RATE_HEADERS[2])
        if limit <= 0 or remaining > limit:
            raise ValueError("Toss rate limit response is inconsistent")
        bucket.reported_limit = limit
        capacity = _capacity(bucket)
        bucket.tokens = float(min(remaining, capacity))
        bucket.refill_seconds = max(reset, 1 / capacity)
        bucket.updated_at = self._monotonic()
        return reset

    async def _sleep_before_deadline(self, delay: float) -> None:
        now = self._monotonic()
        if (
            not math.isfinite(delay)
            or delay < 0
            or not math.isfinite(now)
            or now + delay >= self._deadline
        ):
            raise ValueError("Toss rate limit deadline is unavailable")
        await self._sleep(delay)
        if self._monotonic() >= self._deadline:
            raise ValueError("Toss rate limit deadline is unavailable")


def _capacity(bucket: _Bucket) -> int:
    return min(bucket.documented_limit, bucket.reported_limit)


def _documented_limit(group: TossRateGroup, now: datetime) -> int:
    if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Toss rate limit wall clock is invalid")
    if group is TossRateGroup.ORDER_INFO:
        current = now.astimezone(_KST).time().replace(tzinfo=None)
        if _PEAK_START <= current < _PEAK_END:
            return 3
    return _DOCUMENTED_LIMITS[group]


def _request_group(request: BrokerRequest) -> TossRateGroup:
    if type(request) is not BrokerRequest or "#" in request.path:
        raise ValueError("Toss rate-limited request is invalid")
    path, separator, query = request.path.partition("?")
    pairs = _query_pairs(query) if separator else ()
    headers = _exact_headers(request)
    if path == "/oauth2/token":
        if (
            request.method != "POST"
            or pairs
            or headers != {"content-type": "application/x-www-form-urlencoded"}
            or not request.body
        ):
            raise ValueError("Toss OAuth request is invalid")
        return TossRateGroup.AUTH
    if request.method != "GET" or request.body is not None:
        raise ValueError("Toss read request is invalid")
    if path == "/api/v1/accounts":
        _require_headers(headers, account=False)
        if pairs:
            raise ValueError("Toss accounts request is invalid")
        return TossRateGroup.ACCOUNT
    if path == "/api/v1/holdings":
        _require_headers(headers, account=True)
        if pairs:
            raise ValueError("Toss holdings request is invalid")
        return TossRateGroup.ASSET
    if path == "/api/v1/buying-power":
        _require_headers(headers, account=True)
        if pairs not in ((("currency", "KRW"),), (("currency", "USD"),)):
            raise ValueError("Toss buying-power request is invalid")
        return TossRateGroup.ORDER_INFO
    if path == "/api/v1/sellable-quantity":
        _require_headers(headers, account=True)
        if len(pairs) != 1 or pairs[0][0] != "symbol" or not _stock_symbol(pairs[0][1]):
            raise ValueError("Toss sellable-quantity request is invalid")
        return TossRateGroup.ORDER_INFO
    if path == "/api/v1/orders":
        _require_headers(headers, account=True)
        _require_order_query(pairs)
        return TossRateGroup.ORDER_HISTORY
    if path == "/api/v1/commissions":
        _require_headers(headers, account=True)
        if pairs:
            raise ValueError("Toss commissions request is invalid")
        return TossRateGroup.ORDER_INFO
    if path == "/api/v1/market-calendar/KR":
        _require_headers(headers, account=False)
        if len(pairs) != 1 or pairs[0][0] != "date":
            raise ValueError("Toss market-calendar request is invalid")
        try:
            if datetime.fromisoformat(pairs[0][1]).date().isoformat() != pairs[0][1]:
                raise ValueError("Toss market-calendar date is invalid")
        except ValueError as error:
            raise ValueError("Toss market-calendar request is invalid") from error
        return TossRateGroup.MARKET_INFO
    raise ValueError("Toss rate-limited request route is not approved")


def _query_pairs(query: str) -> tuple[tuple[str, str], ...]:
    try:
        pairs = tuple(parse_qsl(query, keep_blank_values=True, strict_parsing=True))
    except ValueError as error:
        raise ValueError("Toss request query is invalid") from error
    if not pairs or len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("Toss request query is invalid")
    return pairs


def _exact_headers(request: BrokerRequest) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in request.headers:
        normalized = key.casefold()
        if normalized in headers:
            raise ValueError("Toss request header is duplicated")
        headers[normalized] = value
    return headers


def _require_headers(headers: dict[str, str], *, account: bool) -> None:
    expected = {"authorization"}
    if account:
        expected.add("x-tossinvest-account")
    if set(headers) != expected:
        raise ValueError("Toss request headers are invalid")
    authorization = headers["authorization"]
    if not authorization.startswith("Bearer ") or not authorization[7:]:
        raise ValueError("Toss authorization header is invalid")
    if account and not headers["x-tossinvest-account"]:
        raise ValueError("Toss account header is invalid")


def _require_order_query(pairs: tuple[tuple[str, str], ...]) -> None:
    values = dict(pairs)
    status = values.get("status")
    if status not in {"OPEN", "CLOSED"}:
        raise ValueError("Toss order history status is invalid")
    allowed = {"status", "symbol", "from", "to"}
    if status == "CLOSED":
        allowed.update({"cursor", "limit"})
    if not set(values) <= allowed:
        raise ValueError("Toss order history query is invalid")
    if "symbol" in values and not _stock_symbol(values["symbol"]):
        raise ValueError("Toss order history symbol is invalid")
    if ("from" in values) != ("to" in values):
        raise ValueError("Toss order history date range is incomplete")
    if "from" in values:
        start = _date(values["from"])
        end = _date(values["to"])
        if start > end:
            raise ValueError("Toss order history date range is invalid")
    if "limit" in values:
        raw_limit = values["limit"]
        if _INTEGER.fullmatch(raw_limit) is None or not 1 <= int(raw_limit) <= 100:
            raise ValueError("Toss order history limit is invalid")
    if "cursor" in values and (
        not values["cursor"] or "\n" in values["cursor"] or len(values["cursor"]) > 1024
    ):
        raise ValueError("Toss order history cursor is invalid")


def _date(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Toss order history date is invalid") from error
    if parsed.date().isoformat() != value:
        raise ValueError("Toss order history date is invalid")
    return parsed


def _stock_symbol(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 32
        and value.isascii()
        and all(character.isalnum() or character in ".-" for character in value)
    )


def _single_integer_header(response: BrokerResponse, name: str) -> int:
    value = _single_header(response, name)
    if _INTEGER.fullmatch(value) is None:
        raise ValueError("Toss rate limit integer header is invalid")
    return int(value)


def _single_numeric_header(response: BrokerResponse, name: str) -> float:
    value = _single_header(response, name)
    if _NUMBER.fullmatch(value) is None:
        raise ValueError("Toss rate limit numeric header is invalid")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Toss rate limit numeric header is invalid")
    return parsed


def _single_header(response: BrokerResponse, name: str) -> str:
    values = tuple(
        value for key, value in response.headers if key.casefold() == name.casefold()
    )
    if len(values) != 1:
        raise ValueError("Toss rate limit response header is missing or duplicated")
    return values[0]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _scrub_exception(caught: BaseException) -> None:
    caught.__traceback__ = None
    caught.__context__ = None
    caught.__cause__ = None
    caught.args = ()
    caught.__dict__.clear()


def _scrub_control(caught: BaseException) -> None:
    _scrub_exception(caught)
    if isinstance(caught, SystemExit):
        caught.code = 1
