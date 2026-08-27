"""Sessions against a real Redis.

The one-use guarantee on a login state and the absolute session lifetime are
the two things a fake would let pass while the real server did something else.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import integration_redis_url
from redis import asyncio as redis

from autotrader.apps.backoffice.auth import (
    LoginAttempt,
    Operator,
    new_login_attempt,
)
from autotrader.apps.backoffice.sessions import (
    LOGIN_PREFIX,
    SESSION_PREFIX,
    RedisSessionStore,
    SessionIdentityCollisionError,
)

ALLOWED = "operator@example.com"


def _drive(scenario: object) -> None:
    url = integration_redis_url()
    if url is None:
        pytest.skip("a Redis connection is required for integration tests")

    async def run() -> None:
        client = redis.from_url(url, decode_responses=True)
        try:
            await scenario(client)  # type: ignore[operator]
        finally:
            for prefix in (LOGIN_PREFIX, SESSION_PREFIX):
                keys = [key async for key in client.scan_iter(f"{prefix}*")]
                if keys:
                    await client.delete(*keys)
            await client.aclose()

    asyncio.run(run())


@pytest.mark.integration
def test_a_login_state_can_be_spent_exactly_once() -> None:
    async def scenario(client: object) -> None:
        store = RedisSessionStore(client)  # type: ignore[arg-type]
        attempt = new_login_attempt()
        await store.begin_login(attempt)

        first = await store.take_login(attempt.state)
        second = await store.take_login(attempt.state)

        assert first == attempt
        # A replayed callback finds nothing to redeem.
        assert second is None

    _drive(scenario)


@pytest.mark.integration
def test_a_state_nobody_issued_is_not_a_login() -> None:
    async def scenario(client: object) -> None:
        store = RedisSessionStore(client)  # type: ignore[arg-type]

        assert await store.take_login("invented") is None

    _drive(scenario)


@pytest.mark.integration
def test_the_same_state_twice_is_refused_rather_than_overwritten() -> None:
    async def scenario(client: object) -> None:
        store = RedisSessionStore(client)  # type: ignore[arg-type]
        attempt = new_login_attempt()
        await store.begin_login(attempt)

        with pytest.raises(SessionIdentityCollisionError):
            await store.begin_login(
                LoginAttempt(state=attempt.state, nonce="other", code_verifier="other")
            )

    _drive(scenario)


@pytest.mark.integration
def test_a_session_survives_until_it_is_ended() -> None:
    async def scenario(client: object) -> None:
        store = RedisSessionStore(client)  # type: ignore[arg-type]

        session_id = await store.create_session(Operator(email=ALLOWED))

        assert await store.operator_for(session_id) == Operator(email=ALLOWED)
        await store.end_session(session_id)
        assert await store.operator_for(session_id) is None

    _drive(scenario)


@pytest.mark.integration
def test_two_logins_get_two_sessions() -> None:
    async def scenario(client: object) -> None:
        store = RedisSessionStore(client)  # type: ignore[arg-type]

        first = await store.create_session(Operator(email=ALLOWED))
        second = await store.create_session(Operator(email=ALLOWED))

        # Signing in again rotates the identifier rather than reusing it.
        assert first != second

    _drive(scenario)


@pytest.mark.integration
def test_a_session_carries_an_absolute_lifetime() -> None:
    async def scenario(client: object) -> None:
        store = RedisSessionStore(client)  # type: ignore[arg-type]
        session_id = await store.create_session(Operator(email=ALLOWED))

        first = await client.ttl(f"{SESSION_PREFIX}{session_id}")  # type: ignore[attr-defined]
        await store.operator_for(session_id)
        second = await client.ttl(f"{SESSION_PREFIX}{session_id}")  # type: ignore[attr-defined]

        assert 0 < first <= 12 * 60 * 60
        # Reading a session does not slide its expiry, or an open tab would
        # never sign out.
        assert second <= first

    _drive(scenario)


@pytest.mark.integration
def test_a_login_state_expires_far_sooner_than_a_session() -> None:
    async def scenario(client: object) -> None:
        store = RedisSessionStore(client)  # type: ignore[arg-type]
        attempt = new_login_attempt()
        await store.begin_login(attempt)

        ttl = await client.ttl(f"{LOGIN_PREFIX}{attempt.state}")  # type: ignore[attr-defined]

        # It only has to outlive a sign-in, not a working day.
        assert 0 < ttl <= 10 * 60

    _drive(scenario)


@pytest.mark.integration
def test_a_stored_email_comes_back_normalized() -> None:
    async def scenario(client: object) -> None:
        store = RedisSessionStore(client)  # type: ignore[arg-type]

        session_id = await store.create_session(
            Operator(email="  Operator@EXAMPLE.com ")
        )

        assert await store.operator_for(session_id) == Operator(email=ALLOWED)

    _drive(scenario)
