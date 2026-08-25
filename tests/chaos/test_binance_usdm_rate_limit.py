from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from autotrader.integrations.brokers.binance_usdm.rate_limit import (
    BinanceUsdmRateLimiter,
    BinanceUsdmRateLimitError,
)
from autotrader.integrations.brokers.common import BrokerResponse


@dataclass
class _Clock:
    value: float = 0.0
    sleeps: list[float] = field(default_factory=list[float])

    def monotonic(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def _limiter(clock: _Clock) -> BinanceUsdmRateLimiter:
    return BinanceUsdmRateLimiter(
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        default_rate_limit_backoff_seconds=2.0,
        default_ip_ban_backoff_seconds=120.0,
        maximum_backoff_seconds=300.0,
    )


@pytest.mark.asyncio
async def test_captures_all_weight_and_order_count_windows() -> None:
    clock = _Clock()
    limiter = _limiter(clock)

    await limiter.observe_response(
        BrokerResponse(
            200,
            b"{}",
            headers=(
                ("X-MBX-USED-WEIGHT-1M", "17"),
                ("x-mbx-used-weight-5m", "31"),
                ("X-MBX-ORDER-COUNT-10S", "2"),
                ("X-MBX-ORDER-COUNT-1M", "4"),
            ),
        )
    )

    snapshot = limiter.snapshot()
    assert snapshot.used_weight == {"1M": 17, "5M": 31}
    assert snapshot.order_count == {"10S": 2, "1M": 4}
    assert snapshot.blocked_until == 0.0


@pytest.mark.asyncio
async def test_429_retry_after_blocks_every_user_of_the_shared_limiter() -> None:
    clock = _Clock()
    shared = _limiter(clock)

    await shared.observe_response(
        BrokerResponse(
            429,
            b"{}",
            headers=(
                ("X-MBX-USED-WEIGHT-1M", "2400"),
                ("Retry-After", "3"),
            ),
        )
    )
    await shared.before_request()

    assert clock.sleeps == [3.0]
    assert shared.snapshot().blocked_until == 3.0


@pytest.mark.asyncio
async def test_418_without_retry_after_uses_bounded_fail_closed_default() -> None:
    clock = _Clock()
    limiter = _limiter(clock)

    await limiter.observe_response(BrokerResponse(418, b"{}"))
    await limiter.before_request()

    assert clock.sleeps == [120.0]


@pytest.mark.asyncio
async def test_malformed_duplicate_or_excessive_backoff_headers_fail_closed() -> None:
    clock = _Clock()
    limiter = _limiter(clock)
    invalid = (
        BrokerResponse(200, b"{}", (("X-MBX-USED-WEIGHT-1M", "-1"),)),
        BrokerResponse(
            200,
            b"{}",
            (
                ("X-MBX-ORDER-COUNT-1M", "1"),
                ("x-mbx-order-count-1m", "2"),
            ),
        ),
        BrokerResponse(429, b"{}", (("Retry-After", "301"),)),
    )

    for response in invalid:
        with pytest.raises(BinanceUsdmRateLimitError):
            await limiter.observe_response(response)


@pytest.mark.asyncio
async def test_monotonic_clock_regression_fails_closed() -> None:
    clock = _Clock(value=10.0)
    limiter = _limiter(clock)
    await limiter.observe_response(BrokerResponse(429, b"{}"))
    clock.value = 9.0

    with pytest.raises(BinanceUsdmRateLimitError):
        await limiter.before_request()
