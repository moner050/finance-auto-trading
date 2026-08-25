from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from autotrader.integrations.brokers.common import BrokerRequest, BrokerResponse
from autotrader.integrations.brokers.toss.rate_limit import TossRateLimitedTransport


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.wall = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def now(self) -> datetime:
        return self.wall

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds
        self.wall += timedelta(seconds=seconds)


class _Transport:
    def __init__(self, responses: list[BrokerResponse]) -> None:
        self.responses = responses
        self.requests: list[BrokerRequest] = []

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def _response(
    *,
    status: int = 200,
    limit: str = "5",
    remaining: str = "4",
    reset: str = "0.2",
    retry_after: str | None = None,
) -> BrokerResponse:
    headers = [
        ("X-RateLimit-Limit", limit),
        ("X-RateLimit-Remaining", remaining),
        ("X-RateLimit-Reset", reset),
    ]
    if retry_after is not None:
        headers.append(("Retry-After", retry_after))
    return BrokerResponse(status=status, body=b"{}", headers=tuple(headers))


def _request(path: str) -> BrokerRequest:
    return BrokerRequest(
        method="GET",
        path=path,
        headers=(
            ("Authorization", "Bearer private"),
            ("X-Tossinvest-Account", "private-account"),
        ),
    )


def _limited(raw: _Transport, clock: _Clock) -> TossRateLimitedTransport:
    return TossRateLimitedTransport(
        transport=raw,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        wall_clock=clock.now,
        jitter=lambda _: 0.0,
        deadline=30.0,
    )


@pytest.mark.asyncio
async def test_us_reconciliation_routes_are_exactly_approved() -> None:
    paths = (
        "/api/v1/buying-power?currency=USD",
        "/api/v1/sellable-quantity?symbol=AAPL",
        "/api/v1/orders?status=OPEN&from=2026-07-25&to=2026-08-24",
        (
            "/api/v1/orders?status=CLOSED&from=2026-07-25&to=2026-08-24"
            "&limit=100&cursor=opaque"
        ),
        "/api/v1/commissions",
    )
    clock = _Clock()
    raw = _Transport([_response(limit="10", remaining="9") for _ in paths])
    transport = _limited(raw, clock)

    for path in paths:
        assert (await transport.request(_request(path))).status == 200

    assert [request.path for request in raw.requests] == list(paths)


@pytest.mark.asyncio
async def test_open_and_closed_history_share_one_exhaustible_bucket() -> None:
    clock = _Clock()
    raw = _Transport(
        [
            _response(limit="5", remaining="0", reset="0.2"),
            _response(limit="5", remaining="4", reset="0.2"),
        ]
    )
    transport = _limited(raw, clock)

    await transport.request(
        _request("/api/v1/orders?status=OPEN&from=2026-07-25&to=2026-08-24")
    )
    await transport.request(
        _request("/api/v1/orders?status=CLOSED&from=2026-07-25&to=2026-08-24&limit=100")
    )

    assert clock.sleeps == [pytest.approx(0.2)]


@pytest.mark.asyncio
async def test_closed_history_retry_after_is_bounded_to_one_replay() -> None:
    clock = _Clock()
    raw = _Transport(
        [
            _response(
                status=429,
                remaining="0",
                reset="0.25",
                retry_after="0.5",
            ),
            _response(limit="5", remaining="4"),
        ]
    )
    transport = _limited(raw, clock)
    request = _request(
        "/api/v1/orders?status=CLOSED&from=2026-07-25&to=2026-08-24&limit=100"
    )

    assert (await transport.request(request)).status == 200
    assert raw.requests == [request, request]
    assert clock.sleeps == [1.0]
