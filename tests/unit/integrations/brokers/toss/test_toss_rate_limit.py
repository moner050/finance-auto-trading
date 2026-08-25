from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from autotrader.integrations.brokers.common import BrokerRequest, BrokerResponse
from autotrader.integrations.brokers.toss.rate_limit import (
    TossRateLimitedTransport,
    TossRateLimitUnavailable,
)


class _Clock:
    def __init__(self, *, wall: datetime) -> None:
        self.value = 0.0
        self.wall = wall
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def now(self) -> datetime:
        return self.wall

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds
        self.wall += timedelta(seconds=seconds)


class _Responses:
    def __init__(
        self,
        factory: Callable[[int, BrokerRequest], BrokerResponse],
    ) -> None:
        self.factory = factory
        self.requests: list[BrokerRequest] = []

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return self.factory(len(self.requests), request)


class _Canceller:
    def __init__(self, error: asyncio.CancelledError) -> None:
        self.error = error

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        del request
        raise self.error


def _headers(
    *, limit: str = "5", remaining: str = "4", reset: str = "0.2"
) -> tuple[tuple[str, str], ...]:
    return (
        ("X-RateLimit-Limit", limit),
        ("X-RateLimit-Remaining", remaining),
        ("X-RateLimit-Reset", reset),
    )


def _response(
    *,
    status: int = 200,
    limit: str = "5",
    remaining: str = "4",
    reset: str = "0.2",
    extra: tuple[tuple[str, str], ...] = (),
) -> BrokerResponse:
    return BrokerResponse(
        status=status,
        body=b"{}",
        headers=_headers(limit=limit, remaining=remaining, reset=reset) + extra,
    )


def _transport(
    responses: _Responses,
    clock: _Clock,
    *,
    deadline: float = 18.0,
) -> TossRateLimitedTransport:
    return TossRateLimitedTransport(
        transport=responses,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        wall_clock=clock.now,
        jitter=lambda _: 0.0,
        deadline=deadline,
    )


def _oauth_request() -> BrokerRequest:
    return BrokerRequest(
        method="POST",
        path="/oauth2/token",
        headers=(("Content-Type", "application/x-www-form-urlencoded"),),
        body=b"grant_type=client_credentials&client_id=id&client_secret=secret",
    )


def _account_request(path: str) -> BrokerRequest:
    return BrokerRequest(
        method="GET",
        path=path,
        headers=(
            ("Authorization", "Bearer private"),
            ("X-Tossinvest-Account", "private-account"),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broker_request",
    (
        _oauth_request(),
        BrokerRequest(
            method="GET",
            path="/api/v1/accounts",
            headers=(("Authorization", "Bearer private"),),
        ),
        _account_request("/api/v1/holdings"),
        _account_request("/api/v1/buying-power?currency=KRW"),
        _account_request("/api/v1/sellable-quantity?symbol=005930"),
        _account_request("/api/v1/orders?status=OPEN"),
        BrokerRequest(
            method="GET",
            path="/api/v1/market-calendar/KR?date=2026-08-19",
            headers=(("Authorization", "Bearer private"),),
        ),
    ),
)
async def test_exact_recurring_routes_are_forwarded(
    broker_request: BrokerRequest,
) -> None:
    clock = _Clock(wall=datetime(2026, 8, 19, tzinfo=UTC))
    responses = _Responses(lambda _count, _request: _response())

    response = await _transport(responses, clock).request(broker_request)

    assert response.status == 200
    assert responses.requests == [broker_request]


@pytest.mark.asyncio
async def test_order_info_peak_is_paced_at_three_tps() -> None:
    clock = _Clock(wall=datetime(2026, 8, 19, 0, 0, tzinfo=UTC))

    def respond(count: int, _request: BrokerRequest) -> BrokerResponse:
        remaining = max(0, 3 - count)
        return _response(limit="3", remaining=str(remaining), reset=str(1 / 3))

    responses = _Responses(respond)
    transport = _transport(responses, clock)
    for symbol in ("000001", "000002", "000003", "000004"):
        await transport.request(
            _account_request(f"/api/v1/sellable-quantity?symbol={symbol}")
        )

    assert clock.sleeps == [pytest.approx(1 / 3)]


@pytest.mark.asyncio
async def test_order_info_outside_peak_uses_six_tps() -> None:
    clock = _Clock(wall=datetime(2026, 8, 19, 0, 10, tzinfo=UTC))

    def respond(count: int, _request: BrokerRequest) -> BrokerResponse:
        remaining = max(0, 6 - count)
        return _response(limit="6", remaining=str(remaining), reset=str(1 / 6))

    responses = _Responses(respond)
    transport = _transport(responses, clock)
    for index in range(7):
        await transport.request(
            _account_request(f"/api/v1/sellable-quantity?symbol={index + 1:06d}")
        )

    assert clock.sleeps == [pytest.approx(1 / 6)]


@pytest.mark.asyncio
async def test_rate_groups_have_independent_buckets() -> None:
    clock = _Clock(wall=datetime(2026, 8, 19, 0, 0, tzinfo=UTC))
    responses = _Responses(
        lambda _count, request: (
            _response(limit="1", remaining="0", reset="1")
            if request.path == "/api/v1/accounts"
            else _response(limit="5", remaining="4", reset="0.2")
        )
    )
    transport = _transport(responses, clock)

    await transport.request(
        BrokerRequest(
            method="GET",
            path="/api/v1/accounts",
            headers=(("Authorization", "Bearer private"),),
        )
    )
    await transport.request(_account_request("/api/v1/holdings"))

    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_dynamic_limit_only_lowers_and_remaining_zero_waits_for_reset() -> None:
    clock = _Clock(wall=datetime(2026, 8, 19, 0, 10, tzinfo=UTC))
    responses = _Responses(
        lambda _count, _request: _response(limit="2", remaining="0", reset="0.5")
    )
    transport = _transport(responses, clock)

    await transport.request(_account_request("/api/v1/buying-power?currency=KRW"))
    await transport.request(_account_request("/api/v1/buying-power?currency=KRW"))

    assert clock.sleeps == [0.5]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    (
        (),
        (*_headers(), ("X-RateLimit-Limit", "5")),
        _headers(limit="five"),
        _headers(limit="0"),
        _headers(remaining="6"),
        _headers(reset="-1"),
    ),
)
async def test_invalid_dynamic_headers_fail_closed(
    headers: tuple[tuple[str, str], ...],
) -> None:
    clock = _Clock(wall=datetime(2026, 8, 19, 0, 10, tzinfo=UTC))
    responses = _Responses(lambda _count, _request: BrokerResponse(200, b"{}", headers))

    with pytest.raises(TossRateLimitUnavailable):
        await _transport(responses, clock).request(_account_request("/api/v1/holdings"))


@pytest.mark.asyncio
async def test_one_429_retry_is_deadline_bounded() -> None:
    clock = _Clock(wall=datetime(2026, 8, 19, 0, 10, tzinfo=UTC))

    def respond(count: int, _request: BrokerRequest) -> BrokerResponse:
        if count == 1:
            return _response(
                status=429,
                remaining="0",
                reset="0.25",
                extra=(("Retry-After", "0.5"),),
            )
        return _response()

    responses = _Responses(respond)

    result = await _transport(responses, clock).request(
        _account_request("/api/v1/holdings")
    )

    assert result.status == 200
    assert len(responses.requests) == 2
    assert clock.sleeps == [1.0]


@pytest.mark.asyncio
async def test_second_429_fails_closed() -> None:
    clock = _Clock(wall=datetime(2026, 8, 19, 0, 10, tzinfo=UTC))
    responses = _Responses(
        lambda _count, _request: _response(
            status=429,
            remaining="0",
            reset="0.25",
            extra=(("Retry-After", "0.5"),),
        )
    )

    with pytest.raises(TossRateLimitUnavailable):
        await _transport(responses, clock).request(_account_request("/api/v1/holdings"))

    assert len(responses.requests) == 2


@pytest.mark.asyncio
async def test_429_retry_that_crosses_deadline_fails_without_sleep() -> None:
    clock = _Clock(wall=datetime(2026, 8, 19, 0, 10, tzinfo=UTC))
    responses = _Responses(
        lambda _count, _request: _response(
            status=429,
            remaining="0",
            reset="1",
            extra=(("Retry-After", "2"),),
        )
    )

    with pytest.raises(TossRateLimitUnavailable):
        await _transport(responses, clock, deadline=1.5).request(
            _account_request("/api/v1/holdings")
        )

    assert len(responses.requests) == 1
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_cancellation_is_sanitized_and_preserved() -> None:
    error = asyncio.CancelledError("private-token")

    clock = _Clock(wall=datetime(2026, 8, 19, 0, 10, tzinfo=UTC))
    transport = TossRateLimitedTransport(
        transport=_Canceller(error),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        wall_clock=clock.now,
        jitter=lambda _: 0.0,
        deadline=18,
    )

    with pytest.raises(asyncio.CancelledError) as raised:
        await transport.request(_account_request("/api/v1/holdings"))

    assert raised.value is error
    assert error.args == ()
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broker_request",
    (
        _account_request("/api/v1/unknown"),
        BrokerRequest(
            method="GET",
            path="/api/v1/holdings",
            headers=(
                ("Authorization", "Bearer private"),
                ("X-Tossinvest-Account", "private-account"),
                ("X-Unknown", "private"),
            ),
        ),
        BrokerRequest(
            method="GET",
            path="/api/v1/holdings",
            headers=(
                ("Authorization", "Bearer private"),
                ("X-Tossinvest-Account", "private-account"),
            ),
            body=b"private",
        ),
    ),
)
async def test_unapproved_request_shape_is_rejected_before_transport(
    broker_request: BrokerRequest,
) -> None:
    clock = _Clock(wall=datetime(2026, 8, 19, 0, 10, tzinfo=UTC))
    responses = _Responses(lambda _count, _request: _response())

    with pytest.raises(TossRateLimitUnavailable):
        await _transport(responses, clock).request(broker_request)

    assert responses.requests == []
