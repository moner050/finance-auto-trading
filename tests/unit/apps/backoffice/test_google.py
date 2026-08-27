"""Every check on a Google ID token, broken one at a time.

A test that only signs a good token proves the happy path and nothing else.
What matters is that each individual guarantee is load bearing: remove one and
the sign-in must fail. These tests mint their own tokens, so no credential and
no network is involved.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from autotrader.apps.backoffice.auth import (
    BackofficeConfig,
    IdentityUnavailableError,
    LoginAttempt,
)
from autotrader.apps.backoffice.google import (
    ALGORITHMS,
    AUTHORIZATION_ENDPOINT,
    JWKS_URI,
    TOKEN_ENDPOINT,
    GoogleIdentityProvider,
    code_challenge,
)

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
CLIENT_ID = "client-id.apps.googleusercontent.com"
ALLOWED = "operator@example.com"

_SIGNING_KEY = RSAKey.generate_key(2048, parameters={"kid": "signing"})
_OTHER_KEY = RSAKey.generate_key(2048, parameters={"kid": "signing"})


def _b64(payload: Mapping[str, object]) -> str:
    raw = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _config(public_url: str = "https://backoffice.example.com") -> BackofficeConfig:
    return BackofficeConfig(
        public_url=public_url,
        allowed_email=ALLOWED,
        client_id=CLIENT_ID,
        client_secret="client-secret",
        redis_url="redis://localhost:6379/0",
    )


def _attempt() -> LoginAttempt:
    return LoginAttempt(state="state", nonce="nonce", code_verifier="verifier")


def _claims(**changes: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "1234567890",
        "email": ALLOWED,
        "email_verified": True,
        "nonce": "nonce",
        "iat": int((NOW - timedelta(minutes=1)).timestamp()),
        "exp": int((NOW + timedelta(minutes=10)).timestamp()),
    }
    claims.update(changes)
    return claims


def _sign(
    claims: Mapping[str, object], *, key: RSAKey = _SIGNING_KEY, alg: str = "RS256"
) -> str:
    return jwt.encode({"alg": alg, "kid": key.kid}, dict(claims), key)


class _Transport:
    """Answers the two calls, and remembers what it was asked."""

    def __init__(self, id_token: str, *, keys: KeySet | None = None) -> None:
        self._id_token = id_token
        self._keys = keys or KeySet([_SIGNING_KEY])
        self.forms: list[Mapping[str, str]] = []
        self.jwks_calls = 0

    async def get_json(self, url: str) -> Mapping[str, object]:
        assert url == JWKS_URI
        self.jwks_calls += 1
        return self._keys.as_dict()

    async def post_form(
        self, url: str, form: Mapping[str, str]
    ) -> Mapping[str, object]:
        assert url == TOKEN_ENDPOINT
        self.forms.append(form)
        return {"id_token": self._id_token}


def _provider(transport: _Transport, **changes: Any) -> GoogleIdentityProvider:
    return GoogleIdentityProvider(
        config=changes.pop("config", _config()),
        transport=transport,
        now=lambda: NOW,
        **changes,
    )


async def _verify(transport: _Transport, **changes: Any) -> Any:
    return await _provider(transport, **changes).verify(
        code="the-code", attempt=_attempt()
    )


@pytest.mark.asyncio
async def test_a_well_formed_token_admits_the_identity() -> None:
    identity = await _verify(_Transport(_sign(_claims())))

    assert identity.email == ALLOWED
    assert identity.email_verified is True


@pytest.mark.asyncio
async def test_the_exchange_sends_the_verifier_and_never_the_challenge() -> None:
    transport = _Transport(_sign(_claims()))

    await _verify(transport)

    form = transport.forms[0]
    assert form["grant_type"] == "authorization_code"
    assert form["code_verifier"] == "verifier"
    assert form["client_id"] == CLIENT_ID
    # PKCE exists so the verifier reaches the token endpoint and nowhere else.
    assert "code_challenge" not in form


def test_the_authorization_url_carries_a_challenge_and_never_the_verifier() -> None:
    url = _provider(_Transport("unused")).authorization_url(_attempt())

    assert url.startswith(AUTHORIZATION_ENDPOINT)
    assert f"code_challenge={code_challenge('verifier')}" in url
    assert "code_challenge_method=S256" in url
    assert "nonce=nonce" in url
    assert "state=state" in url
    # The verifier travels only over the back channel.
    assert "verifier" not in url


def test_the_challenge_is_the_hash_and_not_the_verifier() -> None:
    challenge = code_challenge("verifier")

    assert challenge != "verifier"
    assert "=" not in challenge
    assert code_challenge("verifier") == challenge
    assert code_challenge("other") != challenge


@pytest.mark.asyncio
async def test_a_token_signed_by_someone_else_is_refused() -> None:
    # Same key id, different key: the identifier is a hint, not a credential.
    transport = _Transport(_sign(_claims(), key=_OTHER_KEY))

    with pytest.raises(IdentityUnavailableError):
        await _verify(transport)


@pytest.mark.asyncio
async def test_an_unsigned_token_is_refused() -> None:
    """Built by hand, because the library will not mint one.

    A decoder that trusts the header's algorithm accepts a token anybody can
    write. Restricting the algorithm at decode time is what stops it.
    """
    segments = (
        _b64({"alg": "none", "kid": "signing"}),
        _b64(_claims()),
        "",
    )
    with pytest.raises(IdentityUnavailableError):
        await _verify(_Transport(".".join(segments)))


@pytest.mark.asyncio
async def test_only_rs256_is_accepted() -> None:
    assert ALGORITHMS == ("RS256",)


@pytest.mark.asyncio
async def test_a_token_for_another_audience_is_refused() -> None:
    """The commonest real attack: a valid Google token, issued to someone
    else's application, presented here."""
    transport = _Transport(
        _sign(_claims(aud="another-client.apps.googleusercontent.com"))
    )

    with pytest.raises(IdentityUnavailableError):
        await _verify(transport)


@pytest.mark.asyncio
async def test_a_token_from_another_issuer_is_refused() -> None:
    transport = _Transport(_sign(_claims(iss="https://accounts.example.com")))

    with pytest.raises(IdentityUnavailableError):
        await _verify(transport)


@pytest.mark.asyncio
async def test_both_spellings_of_the_google_issuer_are_accepted() -> None:
    for issuer in ("https://accounts.google.com", "accounts.google.com"):
        identity = await _verify(_Transport(_sign(_claims(iss=issuer))))
        assert identity.email == ALLOWED


@pytest.mark.asyncio
async def test_an_expired_token_is_refused() -> None:
    transport = _Transport(
        _sign(_claims(exp=int((NOW - timedelta(minutes=1)).timestamp())))
    )

    with pytest.raises(IdentityUnavailableError):
        await _verify(transport)


@pytest.mark.asyncio
async def test_a_token_with_no_expiry_is_refused() -> None:
    claims = _claims()
    del claims["exp"]

    with pytest.raises(IdentityUnavailableError):
        await _verify(_Transport(_sign(claims)))


@pytest.mark.asyncio
async def test_a_token_with_no_issued_at_is_refused() -> None:
    claims = _claims()
    del claims["iat"]

    with pytest.raises(IdentityUnavailableError):
        await _verify(_Transport(_sign(claims)))


@pytest.mark.asyncio
async def test_a_token_minted_for_another_sign_in_is_refused() -> None:
    """Without the nonce, a token obtained in one browser replays in another."""
    transport = _Transport(_sign(_claims(nonce="a-different-sign-in")))

    with pytest.raises(IdentityUnavailableError):
        await _verify(transport)


@pytest.mark.asyncio
async def test_a_token_with_no_nonce_is_refused() -> None:
    claims = _claims()
    del claims["nonce"]

    with pytest.raises(IdentityUnavailableError):
        await _verify(_Transport(_sign(claims)))


@pytest.mark.asyncio
async def test_an_unverified_email_comes_back_unverified() -> None:
    identity = await _verify(_Transport(_sign(_claims(email_verified=False))))

    assert identity.email_verified is False


@pytest.mark.asyncio
async def test_a_string_that_looks_true_is_not_verification() -> None:
    identity = await _verify(_Transport(_sign(_claims(email_verified="true"))))

    assert identity.email_verified is False


@pytest.mark.asyncio
async def test_a_response_with_no_id_token_is_refused() -> None:
    class _Empty(_Transport):
        async def post_form(
            self, url: str, form: Mapping[str, str]
        ) -> Mapping[str, object]:
            del url, form
            return {"access_token": "not what is being asked for"}

    with pytest.raises(IdentityUnavailableError):
        await _verify(_Empty("unused"))


@pytest.mark.asyncio
async def test_a_token_endpoint_that_fails_is_a_refusal_not_a_crash() -> None:
    class _Broken(_Transport):
        async def post_form(
            self, url: str, form: Mapping[str, str]
        ) -> Mapping[str, object]:
            del url, form
            raise RuntimeError("upstream is down")

    with pytest.raises(IdentityUnavailableError, match="identity rejected"):
        await _verify(_Broken("unused"))


@pytest.mark.asyncio
async def test_every_rejection_reads_the_same() -> None:
    messages = set()
    for claims in (
        _claims(aud="another-client"),
        _claims(iss="https://accounts.example.com"),
        _claims(nonce="elsewhere"),
        _claims(exp=int((NOW - timedelta(minutes=1)).timestamp())),
    ):
        with pytest.raises(IdentityUnavailableError) as caught:
            await _verify(_Transport(_sign(claims)))
        messages.add(str(caught.value))

    assert messages == {"identity rejected"}


@pytest.mark.asyncio
async def test_the_key_set_is_cached_between_sign_ins() -> None:
    transport = _Transport(_sign(_claims()))
    provider = _provider(transport)

    await provider.verify(code="one", attempt=_attempt())
    await provider.verify(code="two", attempt=_attempt())

    assert transport.jwks_calls == 1


@pytest.mark.asyncio
async def test_a_rotated_signing_key_is_fetched_once_more() -> None:
    """Google rotates keys. One stale cache miss is routine, not an attack."""
    transport = _Transport(_sign(_claims()), keys=KeySet([_OTHER_KEY]))
    provider = _provider(transport)

    with pytest.raises(IdentityUnavailableError):
        await provider.verify(code="one", attempt=_attempt())

    # Fetched, failed, refetched, failed again. Exactly one retry.
    assert transport.jwks_calls == 2


def test_a_plain_http_redirect_uri_cannot_be_configured_at_all() -> None:
    # The redirect carries an authorization code, and the config refuses the
    # public URL it would be derived from, so the provider never sees one.
    with pytest.raises(IdentityUnavailableError, match="HTTPS off loopback"):
        _config(public_url="http://backoffice.example.com")


def test_loopback_may_still_be_used_for_development() -> None:
    provider = GoogleIdentityProvider(
        config=_config(public_url="http://127.0.0.1:8000"),
        transport=_Transport("unused"),
    )

    assert provider.authorization_url(_attempt()).startswith(AUTHORIZATION_ENDPOINT)
