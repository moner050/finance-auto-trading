"""The backoffice must not have an unauthenticated configuration.

These are the tests that would still matter if every other page were deleted.
A dashboard nobody can reach is an inconvenience; a dashboard anybody can
reach hands over an account.
"""

from __future__ import annotations

import pytest

from autotrader.apps.backoffice.auth import (
    BackofficeConfig,
    IdentityUnavailableError,
    LoginAttempt,
    Operator,
    VerifiedIdentity,
    admitted_operator,
    new_login_attempt,
    new_session_id,
    normalize_email,
)

ALLOWED = "operator@example.com"


def _config(**changes: str) -> BackofficeConfig:
    values: dict[str, str] = {
        "public_url": "https://backoffice.example.com",
        "allowed_email": ALLOWED,
        "client_id": "client",
        "client_secret": "secret",
        "redis_url": "redis://localhost:6379/0",
    }
    values.update(changes)
    return BackofficeConfig(**values)


@pytest.mark.parametrize(
    "field",
    ("public_url", "allowed_email", "client_id", "client_secret", "redis_url"),
)
def test_a_missing_piece_of_configuration_refuses_to_build(field: str) -> None:
    # Not a warning and not a default. There is no partially configured
    # backoffice, because a partially configured one still answers requests.
    with pytest.raises(IdentityUnavailableError, match=field):
        _config(**{field: "   "})


def test_a_plain_http_public_url_off_loopback_is_refused() -> None:
    # The OIDC redirect carries an authorization code in the URL.
    with pytest.raises(IdentityUnavailableError, match="HTTPS, or HTTP on loopback"):
        _config(public_url="http://backoffice.example.com")


def test_loopback_may_skip_tls_and_says_so_in_the_cookie() -> None:
    config = _config(public_url="http://127.0.0.1:8000")

    assert config.secure_cookie is False
    # A Secure cookie over plain HTTP is never sent back, so claiming it
    # would break the session rather than protect it.
    assert _config().secure_cookie is True


def test_the_redirect_uri_is_derived_rather_than_configured() -> None:
    # One fewer value to get wrong, and it cannot disagree with the host the
    # cookie is scoped to.
    assert _config().redirect_uri == "https://backoffice.example.com/auth/callback"
    assert (
        _config(public_url="https://backoffice.example.com/").redirect_uri
        == "https://backoffice.example.com/auth/callback"
    )


def test_an_unnormalized_allowed_email_is_refused() -> None:
    # Otherwise the comparison at login silently never matches.
    with pytest.raises(IdentityUnavailableError, match="normalized"):
        _config(allowed_email="Operator@Example.com")


def test_the_allowed_operator_is_admitted() -> None:
    identity = VerifiedIdentity(email=ALLOWED, email_verified=True)

    assert admitted_operator(identity, config=_config()) == Operator(email=ALLOWED)


def test_casing_and_space_are_not_a_different_person() -> None:
    identity = VerifiedIdentity(email="  Operator@Example.COM ", email_verified=True)

    assert admitted_operator(identity, config=_config()).email == ALLOWED


def test_another_email_is_refused() -> None:
    identity = VerifiedIdentity(email="someone@example.com", email_verified=True)

    with pytest.raises(IdentityUnavailableError):
        admitted_operator(identity, config=_config())


def test_an_unverified_email_is_refused_even_when_it_is_the_right_one() -> None:
    # Anyone can create an account claiming an address they do not control.
    identity = VerifiedIdentity(email=ALLOWED, email_verified=False)

    with pytest.raises(IdentityUnavailableError):
        admitted_operator(identity, config=_config())


def test_every_rejection_reads_the_same() -> None:
    """Telling a caller which check failed hands them the missing half."""
    rejections = []
    for identity in (
        VerifiedIdentity(email="someone@example.com", email_verified=True),
        VerifiedIdentity(email=ALLOWED, email_verified=False),
        VerifiedIdentity(email="someone@example.com", email_verified=False),
    ):
        with pytest.raises(IdentityUnavailableError) as caught:
            admitted_operator(identity, config=_config())
        rejections.append(str(caught.value))

    assert len(set(rejections)) == 1


def test_a_login_attempt_is_unguessable_and_never_repeats() -> None:
    attempts = [new_login_attempt() for _ in range(64)]

    assert len({attempt.state for attempt in attempts}) == 64
    assert len({attempt.nonce for attempt in attempts}) == 64
    assert len({attempt.code_verifier for attempt in attempts}) == 64
    # A state that doubled as the nonce would defeat the point of having both.
    assert all(
        len({attempt.state, attempt.nonce, attempt.code_verifier}) == 3
        for attempt in attempts
    )
    assert all(len(attempt.state) >= 32 for attempt in attempts)


def test_session_ids_do_not_repeat() -> None:
    assert len({new_session_id() for _ in range(64)}) == 64


def test_normalizing_an_email_is_only_casing_and_space() -> None:
    assert normalize_email("  A.B+tag@Example.com ") == "a.b+tag@example.com"


def test_a_login_attempt_carries_all_three_secrets() -> None:
    attempt = LoginAttempt(state="s", nonce="n", code_verifier="v")

    assert (attempt.state, attempt.nonce, attempt.code_verifier) == ("s", "n", "v")
