"""Sign in with Google, and prove it before believing any of it.

An ID token is a claim until every part of it has been checked: who signed it,
who it was issued to, who issued it, when, and whether it belongs to the
sign-in this browser actually started. Skipping any one of those turns the
whole flow into a formality — an attacker who can present a well-formed token
for a different audience walks in.

Every rejection raises the same error with the same message. Telling a caller
which check failed hands them the one fact they were missing.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from urllib.parse import urlencode

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet, KeySetSerialization

from autotrader.apps.backoffice.auth import (
    BackofficeConfig,
    IdentityUnavailableError,
    LoginAttempt,
    VerifiedIdentity,
)

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
# Google issues both spellings and has for years.
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
SCOPE = "openid email"
GRANT_TYPE = "authorization_code"
# RS256 only. Leaving the algorithm to the token lets it choose "none", or an
# HMAC verified with a public key everyone has.
ALGORITHMS = ("RS256",)
KEY_CACHE_TTL = timedelta(hours=1)
CLOCK_LEEWAY_SECONDS = 30

_REJECTED = "identity rejected"


class HttpsTransport(Protocol):
    """The two calls this flow makes, behind a port so tests need no network."""

    async def get_json(self, url: str) -> Mapping[str, object]: ...

    async def post_form(
        self, url: str, form: Mapping[str, str]
    ) -> Mapping[str, object]: ...


@dataclass(slots=True)
class _CachedKeys:
    key_set: KeySet
    fetched_at: datetime


def code_challenge(code_verifier: str) -> str:
    """S256, which is the only method this flow offers.

    The plain method sends the verifier itself, which is the thing PKCE exists
    to avoid sending.
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class GoogleIdentityProvider:
    def __init__(
        self,
        *,
        config: BackofficeConfig,
        transport: HttpsTransport,
        now: Callable[[], datetime] | None = None,
        key_cache_ttl: timedelta = KEY_CACHE_TTL,
    ) -> None:
        # The redirect URI is derived from the public URL, which
        # BackofficeConfig has already constrained to HTTPS off loopback.
        # Repeating the check here would add a branch no test can reach.
        self._config = config
        self._transport = transport
        self._now = now or (lambda: datetime.now(UTC))
        self._key_cache_ttl = key_cache_ttl
        self._keys: _CachedKeys | None = None

    def authorization_url(self, attempt: LoginAttempt) -> str:
        query = urlencode(
            {
                "client_id": self._config.client_id,
                "redirect_uri": self._config.redirect_uri,
                "response_type": "code",
                "scope": SCOPE,
                "state": attempt.state,
                "nonce": attempt.nonce,
                "code_challenge": code_challenge(attempt.code_verifier),
                "code_challenge_method": "S256",
                # No refresh token is wanted. Nothing here acts on the
                # operator's behalf once they have closed the tab.
                "access_type": "online",
                "prompt": "select_account",
            }
        )
        return f"{AUTHORIZATION_ENDPOINT}?{query}"

    async def verify(self, *, code: str, attempt: LoginAttempt) -> VerifiedIdentity:
        payload = await self._exchange(code, attempt)
        raw = payload.get("id_token")
        if not isinstance(raw, str) or not raw:
            raise IdentityUnavailableError(_REJECTED)
        claims = await self._claims(raw, attempt)
        email = claims.get("email")
        verified = claims.get("email_verified")
        if not isinstance(email, str) or not email:
            raise IdentityUnavailableError(_REJECTED)
        # Exactly the boolean. A provider that answers "false" as a non-empty
        # string would otherwise read as verified.
        return VerifiedIdentity(email=email, email_verified=verified is True)

    async def _exchange(self, code: str, attempt: LoginAttempt) -> Mapping[str, object]:
        if type(code) is not str or not code:
            raise IdentityUnavailableError(_REJECTED)
        try:
            return await self._transport.post_form(
                TOKEN_ENDPOINT,
                {
                    "code": code,
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "redirect_uri": self._config.redirect_uri,
                    "grant_type": GRANT_TYPE,
                    "code_verifier": attempt.code_verifier,
                },
            )
        except IdentityUnavailableError:
            raise
        except Exception as error:
            raise IdentityUnavailableError(_REJECTED) from error

    async def _claims(self, raw: str, attempt: LoginAttempt) -> Mapping[str, object]:
        try:
            token = jwt.decode(raw, await self._key_set(), algorithms=list(ALGORITHMS))
        except JoseError:
            # Google rotates signing keys, so one stale cache miss is expected
            # rather than an attack. It gets exactly one retry.
            try:
                token = jwt.decode(
                    raw, await self._key_set(refresh=True), algorithms=list(ALGORITHMS)
                )
            except JoseError as error:
                raise IdentityUnavailableError(_REJECTED) from error
        registry = jwt.JWTClaimsRegistry(
            now=int(self._now().timestamp()),
            leeway=CLOCK_LEEWAY_SECONDS,
            iss={"essential": True, "values": list(ISSUERS)},
            aud={"essential": True, "values": [self._config.client_id]},
            exp={"essential": True},
            iat={"essential": True},
            # The nonce ties this token to the sign-in this browser started.
            # Without it a token minted for another session replays here.
            nonce={"essential": True, "values": [attempt.nonce]},
        )
        try:
            registry.validate(token.claims)
        except JoseError as error:
            raise IdentityUnavailableError(_REJECTED) from error
        return cast("Mapping[str, object]", token.claims)

    async def _key_set(self, *, refresh: bool = False) -> KeySet:
        cached = self._keys
        if (
            not refresh
            and cached is not None
            and self._now() - cached.fetched_at < self._key_cache_ttl
        ):
            return cached.key_set
        try:
            payload = await self._transport.get_json(JWKS_URI)
            key_set = KeySet.import_key_set(cast("KeySetSerialization", dict(payload)))
        except Exception as error:
            raise IdentityUnavailableError(_REJECTED) from error
        self._keys = _CachedKeys(key_set=key_set, fetched_at=self._now())
        return key_set


__all__ = (
    "ALGORITHMS",
    "AUTHORIZATION_ENDPOINT",
    "GRANT_TYPE",
    "ISSUERS",
    "JWKS_URI",
    "KEY_CACHE_TTL",
    "SCOPE",
    "TOKEN_ENDPOINT",
    "GoogleIdentityProvider",
    "HttpsTransport",
    "code_challenge",
)
