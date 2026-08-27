"""Server-side sessions, held in Redis.

The browser gets an opaque id and nothing else. Everything that decides
anything lives here, so losing Redis logs everyone out — which is the failure
this shape is chosen for. A signed cookie would keep authorizing requests
after the server had forgotten the session existed.

A login attempt is spent by reading it. The read and the delete are one Redis
command rather than two, because two would let a replayed callback slip
between them.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from typing import Protocol, cast

from autotrader.apps.backoffice.auth import (
    LOGIN_LIFETIME,
    SESSION_LIFETIME,
    LoginAttempt,
    Operator,
    new_session_id,
    normalize_email,
)

LOGIN_PREFIX = "backoffice:login:"
SESSION_PREFIX = "backoffice:session:"


class RedisSessionClient(Protocol):
    """The subset of redis-py this store uses."""

    def set(
        self, name: str, value: str, *, ex: int, nx: bool
    ) -> Awaitable[bool | None]: ...

    def getdel(self, name: str) -> Awaitable[str | bytes | None]: ...

    def get(self, name: str) -> Awaitable[str | bytes | None]: ...

    def delete(self, *names: str) -> Awaitable[int]: ...


class SessionIdentityCollisionError(RuntimeError):
    """Raised when a generated identifier is already taken."""


class RedisSessionStore:
    def __init__(self, client: RedisSessionClient) -> None:
        self._client = client

    async def begin_login(self, attempt: LoginAttempt) -> None:
        stored = await self._client.set(
            f"{LOGIN_PREFIX}{attempt.state}",
            json.dumps(
                {"nonce": attempt.nonce, "code_verifier": attempt.code_verifier},
                sort_keys=True,
                separators=(",", ":"),
            ),
            ex=int(LOGIN_LIFETIME.total_seconds()),
            nx=True,
        )
        if not stored:
            # A state is unguessable, so a collision means something is wrong
            # with how they are made rather than bad luck worth retrying.
            raise SessionIdentityCollisionError("login state is already in flight")

    async def take_login(self, state: str) -> LoginAttempt | None:
        raw = await self._client.getdel(f"{LOGIN_PREFIX}{state}")
        if raw is None:
            return None
        payload = _payload(raw)
        return LoginAttempt(
            state=state,
            nonce=str(payload["nonce"]),
            code_verifier=str(payload["code_verifier"]),
        )

    async def create_session(self, operator: Operator) -> str:
        session_id = new_session_id()
        stored = await self._client.set(
            f"{SESSION_PREFIX}{session_id}",
            json.dumps({"email": normalize_email(operator.email)}),
            ex=int(SESSION_LIFETIME.total_seconds()),
            nx=True,
        )
        if not stored:
            raise SessionIdentityCollisionError("session identity is already taken")
        return session_id

    async def operator_for(self, session_id: str) -> Operator | None:
        raw = await self._client.get(f"{SESSION_PREFIX}{session_id}")
        if raw is None:
            return None
        # Deliberately not refreshed on use. The design gives a session a
        # twelve hour absolute lifetime, and sliding it would mean an open tab
        # never expires.
        return Operator(email=str(_payload(raw)["email"]))

    async def end_session(self, session_id: str) -> None:
        await self._client.delete(f"{SESSION_PREFIX}{session_id}")


def _payload(raw: str | bytes) -> dict[str, object]:
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("stored session payload must be an object")
    return cast("dict[str, object]", payload)


__all__ = (
    "LOGIN_PREFIX",
    "SESSION_PREFIX",
    "RedisSessionClient",
    "RedisSessionStore",
    "SessionIdentityCollisionError",
)
