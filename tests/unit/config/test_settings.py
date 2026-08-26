"""How Settings turns operator configuration into connection URLs.

The deployment configures components, not URLs, and every one of them has to
survive the round trip. A dropped credential does not fail loudly: it produces
a URL that connects to the wrong thing, or connects with no authentication at
all.
"""

from __future__ import annotations

import pytest

from autotrader.config.settings import Settings

_CONFIGURED = (
    "DATABASE_URL",
    "MYSQL_HOST",
    "MYSQL_PORT",
    "MYSQL_DATABASE",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "REDIS_URL",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_PW",
)


@pytest.fixture(autouse=True)
def _unconfigured_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings reads the process environment, so a developer's own .env
    would otherwise decide what these tests see."""
    for name in _CONFIGURED:
        monkeypatch.delenv(name, raising=False)


def test_the_mysql_url_is_built_from_its_components() -> None:
    settings = Settings(
        mysql_host="db.example.com",
        mysql_port=3306,
        mysql_database="finance",
        mysql_user="trader",
        mysql_password="s3cret",  # type: ignore[arg-type]
    )

    assert settings.database_connection_url == (
        "mysql+aiomysql://trader:s3cret@db.example.com:3306/finance"
    )


def test_the_redis_url_keeps_the_password() -> None:
    settings = Settings(
        redis_host="cache.example.com",
        redis_port=6379,
        REDIS_PW="s3cret",  # type: ignore[call-arg]
    )

    # Redis authenticates with a password and no username, which is exactly
    # the shape a URL builder is most likely to drop.
    assert settings.redis_connection_url == "redis://:s3cret@cache.example.com:6379"


def test_a_redis_password_with_url_characters_is_escaped() -> None:
    settings = Settings(
        redis_host="cache.example.com",
        redis_port=6379,
        REDIS_PW="p@ss/w:rd#1",  # type: ignore[call-arg]
    )

    assert settings.redis_connection_url == (
        "redis://:p%40ss%2Fw%3Ard%231@cache.example.com:6379"
    )


def test_an_explicit_url_is_never_overridden_by_components() -> None:
    settings = Settings(
        redis_url="redis://elsewhere:6379",
        redis_host="cache.example.com",
        redis_port=6379,
        REDIS_PW="s3cret",  # type: ignore[call-arg]
    )

    assert settings.redis_connection_url == "redis://elsewhere:6379"


def test_half_configured_redis_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="REDIS_HOST, REDIS_PORT, and REDIS_PW"):
        Settings(redis_host="cache.example.com")


def test_no_redis_configuration_at_all_is_not_an_error() -> None:
    assert Settings().redis_connection_url is None
