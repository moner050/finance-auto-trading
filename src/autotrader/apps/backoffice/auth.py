"""Who may reach the backoffice, and what happens when nobody has said.

The one rule this file exists to enforce is that there is no configuration in
which the backoffice serves a page to an unidentified visitor. Not a debug
flag, not a missing environment variable, not a provider that failed to load.
An application that starts without an identity provider and answers requests
anyway is the failure mode every other control here is downstream of, so the
factory refuses to build one.

Sessions live in Redis and the cookie carries an opaque id, so losing Redis
logs everyone out rather than leaving a signed cookie standing on its own.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from autotrader.shared.origins import (
    InvalidOriginError,
    is_loopback,
    require_public_origin,
)

SESSION_COOKIE = "autotrader_backoffice"
SESSION_PATH = "/"
SESSION_LIFETIME = timedelta(hours=12)
# Long enough that a person can finish signing in, short enough that a state
# value left in a browser tab overnight is worthless.
LOGIN_LIFETIME = timedelta(minutes=10)
_SESSION_ID_BYTES = 32


class IdentityUnavailableError(RuntimeError):
    """Raised when the backoffice cannot tell who is asking."""


@dataclass(frozen=True, slots=True)
class Operator:
    """The one person this backoffice answers to."""

    email: str


@dataclass(frozen=True, slots=True)
class BackofficeConfig:
    """Everything that has to be true before a page may be served.

    Validation happens here rather than at the first request, because a server
    that accepts a connection and only then discovers it cannot authenticate
    has already exposed the port.
    """

    public_url: str
    allowed_email: str
    client_id: str
    client_secret: str
    redis_url: str

    def __post_init__(self) -> None:
        for name in (
            "public_url",
            "allowed_email",
            "client_id",
            "client_secret",
            "redis_url",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():
                raise IdentityUnavailableError(f"{name} is required")
        try:
            require_public_origin(self.public_url, name="public_url")
        except InvalidOriginError as error:
            raise IdentityUnavailableError(str(error)) from error
        if normalize_email(self.allowed_email) != self.allowed_email:
            raise IdentityUnavailableError("allowed_email must be normalized")

    @property
    def secure_cookie(self) -> bool:
        """Loopback is the only place the cookie may travel bare.

        A Secure cookie is never sent back over plain HTTP, so claiming it
        there would break the session rather than protect it.
        """
        return not is_loopback(self.public_url)

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url.rstrip('/')}/auth/callback"


def normalize_email(email: str) -> str:
    """Casing and surrounding space are not identity."""
    return email.strip().lower()


@dataclass(frozen=True, slots=True)
class LoginAttempt:
    """One sign-in in flight, held server side until the callback returns."""

    state: str
    nonce: str
    code_verifier: str


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """What the provider proved, before this application decides anything."""

    email: str
    email_verified: bool


class IdentityProvider(Protocol):
    def authorization_url(self, attempt: LoginAttempt) -> str:
        """Where to send the browser to sign in."""
        ...

    async def verify(self, *, code: str, attempt: LoginAttempt) -> VerifiedIdentity:
        """Exchange the code and validate the token, or raise."""
        ...


class SessionStore(Protocol):
    async def begin_login(self, attempt: LoginAttempt) -> None: ...

    async def take_login(self, state: str) -> LoginAttempt | None:
        """Consume the attempt. A state may be spent exactly once."""
        ...

    async def create_session(self, operator: Operator) -> str:
        """Store the session and return its opaque id."""
        ...

    async def operator_for(self, session_id: str) -> Operator | None: ...

    async def end_session(self, session_id: str) -> None: ...


def new_login_attempt() -> LoginAttempt:
    return LoginAttempt(
        state=secrets.token_urlsafe(_SESSION_ID_BYTES),
        nonce=secrets.token_urlsafe(_SESSION_ID_BYTES),
        code_verifier=secrets.token_urlsafe(_SESSION_ID_BYTES),
    )


def new_session_id() -> str:
    return secrets.token_urlsafe(_SESSION_ID_BYTES)


def admitted_operator(
    identity: VerifiedIdentity, *, config: BackofficeConfig
) -> Operator:
    """The operator this identity stands for, or nothing at all.

    Every rejection raises the same error with the same message. Telling a
    caller that the email was wrong but the signature was fine hands them the
    one fact they were missing.
    """
    if not identity.email_verified:
        raise IdentityUnavailableError("identity rejected")
    email = normalize_email(identity.email)
    if not secrets.compare_digest(email, config.allowed_email):
        raise IdentityUnavailableError("identity rejected")
    return Operator(email=email)


__all__ = (
    "LOGIN_LIFETIME",
    "SESSION_COOKIE",
    "SESSION_LIFETIME",
    "SESSION_PATH",
    "BackofficeConfig",
    "IdentityProvider",
    "IdentityUnavailableError",
    "LoginAttempt",
    "Operator",
    "SessionStore",
    "VerifiedIdentity",
    "admitted_operator",
    "new_login_attempt",
    "new_session_id",
    "normalize_email",
)
