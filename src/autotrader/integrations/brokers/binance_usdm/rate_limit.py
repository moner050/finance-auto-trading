from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from autotrader.integrations.brokers.common import BrokerResponse

_COUNTER = re.compile(
    r"x-mbx-(used-weight|order-count)-([1-9][0-9]*[smhd])\Z",
    re.IGNORECASE,
)
_UNSIGNED_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_UNSIGNED_NUMBER = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


class BinanceUsdmRateLimitError(RuntimeError):
    """Raised when Binance rate state cannot be handled without widening risk."""


@dataclass(frozen=True, slots=True)
class BinanceUsdmRateLimitSnapshot:
    used_weight: Mapping[str, int]
    order_count: Mapping[str, int]
    blocked_until: float


class BinanceUsdmRateLimiter:
    """One shared fail-closed view of Binance IP and account rate state."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        default_rate_limit_backoff_seconds: float = 2.0,
        default_ip_ban_backoff_seconds: float = 120.0,
        maximum_backoff_seconds: float = 300.0,
    ) -> None:
        start = monotonic()
        values = (
            start,
            default_rate_limit_backoff_seconds,
            default_ip_ban_backoff_seconds,
            maximum_backoff_seconds,
        )
        if (
            any(
                type(value) not in {int, float} or not math.isfinite(value)
                for value in values
            )
            or start < 0
            or default_rate_limit_backoff_seconds <= 0
            or default_ip_ban_backoff_seconds <= 0
            or maximum_backoff_seconds <= 0
            or default_rate_limit_backoff_seconds > maximum_backoff_seconds
            or default_ip_ban_backoff_seconds > maximum_backoff_seconds
            or not callable(sleep)
        ):
            raise ValueError("Binance USD-M rate limiter configuration is invalid")
        self._monotonic = monotonic
        self._sleep = sleep
        self._rate_backoff = float(default_rate_limit_backoff_seconds)
        self._ban_backoff = float(default_ip_ban_backoff_seconds)
        self._maximum_backoff = float(maximum_backoff_seconds)
        self._last_clock = float(start)
        self._blocked_until = 0.0
        self._used_weight: dict[str, int] = {}
        self._order_count: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def before_request(self) -> None:
        async with self._lock:
            now = self._clock()
            delay = max(0.0, self._blocked_until - now)
            if delay:
                await self._sleep(delay)
                now = self._clock()
                if now + 1e-9 < self._blocked_until:
                    raise BinanceUsdmRateLimitError(
                        "Binance USD-M rate-limit wait did not complete"
                    )

    async def observe_response(self, response: BrokerResponse) -> None:
        if type(response) is not BrokerResponse:
            raise TypeError("Binance USD-M rate response must be exact")
        async with self._lock:
            now = self._clock()
            used_weight, order_count, retry_after = _response_headers(response)
            self._used_weight.update(used_weight)
            self._order_count.update(order_count)
            if response.status not in {418, 429}:
                return
            fallback = (
                self._ban_backoff if response.status == 418 else self._rate_backoff
            )
            delay = fallback if retry_after is None else retry_after
            if delay <= 0 or delay > self._maximum_backoff:
                raise BinanceUsdmRateLimitError(
                    "Binance USD-M provider backoff is invalid"
                )
            self._blocked_until = max(self._blocked_until, now + delay)

    def snapshot(self) -> BinanceUsdmRateLimitSnapshot:
        return BinanceUsdmRateLimitSnapshot(
            used_weight=MappingProxyType(dict(self._used_weight)),
            order_count=MappingProxyType(dict(self._order_count)),
            blocked_until=self._blocked_until,
        )

    def _clock(self) -> float:
        now = self._monotonic()
        if (
            type(now) not in {int, float}
            or not math.isfinite(now)
            or now < self._last_clock
        ):
            raise BinanceUsdmRateLimitError("Binance USD-M monotonic clock is invalid")
        self._last_clock = float(now)
        return float(now)


def _response_headers(
    response: BrokerResponse,
) -> tuple[dict[str, int], dict[str, int], float | None]:
    used_weight: dict[str, int] = {}
    order_count: dict[str, int] = {}
    retry_after_values: list[str] = []
    for name, value in response.headers:
        normalized = name.casefold()
        if normalized == "retry-after":
            retry_after_values.append(value)
            continue
        match = _COUNTER.fullmatch(normalized)
        if match is None:
            continue
        target = (
            used_weight if match.group(1).casefold() == "used-weight" else order_count
        )
        window = match.group(2).upper()
        if window in target or _UNSIGNED_INTEGER.fullmatch(value) is None:
            raise BinanceUsdmRateLimitError(
                "Binance USD-M rate-limit counter header is invalid"
            )
        target[window] = int(value)
    if len(retry_after_values) > 1:
        raise BinanceUsdmRateLimitError("Binance USD-M retry-after header is invalid")
    if not retry_after_values:
        return used_weight, order_count, None
    value = retry_after_values[0]
    if _UNSIGNED_NUMBER.fullmatch(value) is None:
        raise BinanceUsdmRateLimitError("Binance USD-M retry-after header is invalid")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise BinanceUsdmRateLimitError("Binance USD-M retry-after header is invalid")
    return used_weight, order_count, parsed


__all__ = (
    "BinanceUsdmRateLimitError",
    "BinanceUsdmRateLimitSnapshot",
    "BinanceUsdmRateLimiter",
)
