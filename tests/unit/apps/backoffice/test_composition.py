"""Reading the configuration, and refusing to start without it."""

from __future__ import annotations

import pytest

from autotrader.apps.backoffice.auth import IdentityUnavailableError
from autotrader.apps.backoffice.composition import backoffice_config
from autotrader.config.settings import Settings

_CONFIGURED = (
    "BACKOFFICE_PUBLIC_URL",
    "BACKOFFICE_ALLOWED_EMAIL",
    "OAUTH_GOOGLE_CLIENT_ID",
    "OAUTH_GOOGLE_CLIENT_SECRET",
    "REDIS_URL",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_PW",
    "BACKOFFICE_MASTER_KEY",
    "BACKOFFICE_MASTER_KEY_VERSION",
)


@pytest.fixture(autouse=True)
def _unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings reads the process environment, so a developer's own .env
    would otherwise decide what these tests see."""
    for name in _CONFIGURED:
        monkeypatch.delenv(name, raising=False)


def _settings(**changes: object) -> Settings:
    values: dict[str, object] = {
        "backoffice_public_url": "https://backoffice.example.com",
        # Settings couples the public URL to the secret-store master key, so
        # a backoffice cannot be configured without one.
        "backoffice_master_key": "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=",
        "backoffice_master_key_version": 1,
        "backoffice_allowed_email": "operator@example.com",
        "oauth_google_client_id": "client-id.apps.googleusercontent.com",
        "oauth_google_client_secret": "client-secret",
        "redis_url": "redis://localhost:6379/0",
    }
    values.update(changes)
    return Settings(**values)  # type: ignore[arg-type]


def test_a_complete_configuration_is_read() -> None:
    config = backoffice_config(_settings())

    assert config.allowed_email == "operator@example.com"
    assert config.redirect_uri == "https://backoffice.example.com/auth/callback"


def test_the_allowed_email_is_normalized_on_the_way_in() -> None:
    # Otherwise a capital letter in .env silently locks the operator out.
    config = backoffice_config(
        _settings(backoffice_allowed_email="Operator@Example.com")
    )

    assert config.allowed_email == "operator@example.com"


@pytest.mark.parametrize(
    ("field", "named"),
    (
        ("backoffice_allowed_email", "BACKOFFICE_ALLOWED_EMAIL"),
        ("oauth_google_client_id", "OAUTH_GOOGLE_CLIENT_ID"),
        ("oauth_google_client_secret", "OAUTH_GOOGLE_CLIENT_SECRET"),
        ("redis_url", "REDIS_HOST"),
    ),
)
def test_a_missing_value_names_itself_and_stops_the_build(
    field: str, named: str
) -> None:
    with pytest.raises(IdentityUnavailableError, match=named):
        backoffice_config(_settings(**{field: None}))


def test_the_client_secret_never_appears_in_the_error() -> None:
    with pytest.raises(IdentityUnavailableError) as caught:
        backoffice_config(_settings(oauth_google_client_id=None))

    assert "client-secret" not in str(caught.value)
