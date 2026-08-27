"""The plaintext must have nowhere to go.

Section 8.3 lists the paths it must never take: JSON, templates, logs,
exception messages, audit details, debug representations. Each of those is a
call to str, repr, format or a serializer, so each is tested as one.
"""

from __future__ import annotations

import json
import logging

import pytest
from jinja2 import Template

from autotrader.apps.backoffice.secrets import (
    OAUTH,
    PROVIDER_CREDENTIAL,
    Secret,
    SecretReferenceError,
    SecretScope,
    parse_reference,
    secret_aad,
)

VALUE = "the-actual-secret-value"


def test_the_value_comes_back_when_it_is_asked_for() -> None:
    assert Secret(VALUE).reveal() == VALUE


def test_printing_it_shows_nothing() -> None:
    secret = Secret(VALUE)

    assert VALUE not in str(secret)
    assert VALUE not in repr(secret)
    assert VALUE not in f"{secret}"
    assert VALUE not in f"{secret!r}"
    assert VALUE not in format(secret, ">40")


def test_a_template_cannot_render_it() -> None:
    # A backoffice page is the most likely accident.
    rendered = Template("{{ secret }} {{ secret|string }}").render(secret=Secret(VALUE))

    assert VALUE not in rendered


def test_json_refuses_it_rather_than_encoding_it() -> None:
    with pytest.raises(TypeError):
        json.dumps({"secret": Secret(VALUE)})


def test_a_log_line_shows_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        logging.getLogger(__name__).info("resolved %s", Secret(VALUE))

    assert VALUE not in caplog.text


def test_an_exception_message_shows_nothing() -> None:
    try:
        raise ValueError(f"could not use {Secret(VALUE)}")
    except ValueError as error:
        assert VALUE not in str(error)


def test_it_cannot_be_compared_against_a_plaintext() -> None:
    # Comparing is how a value ends up in an assertion message.
    secret = Secret(VALUE)
    plaintext = VALUE

    assert (secret == plaintext) is False
    # And the other way round: NotImplemented on both sides falls back to
    # identity, which two different objects never satisfy.
    assert (plaintext == secret) is False


def test_it_cannot_be_a_dictionary_key() -> None:
    with pytest.raises(TypeError):
        {Secret(VALUE): "somewhere it would be kept"}


def test_an_empty_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        Secret("")


def test_the_reference_has_exactly_one_form() -> None:
    assert parse_reference("secret://db/google-client-secret@active") == (
        "google-client-secret"
    )


@pytest.mark.parametrize(
    "reference",
    (
        "secret://db/name",
        "secret://db/name@latest",
        "secret://db/name@active ",
        "secret://db/NAME@active",
        "secret://file/name@active",
        "db/name@active",
        "",
        "secret://db/@active",
    ),
)
def test_anything_else_is_refused(reference: str) -> None:
    # A resolver that accepted several forms would be handed one that means
    # something slightly different somewhere else.
    with pytest.raises(SecretReferenceError):
        parse_reference(reference)


def test_the_aad_binds_name_version_provider_and_environment() -> None:
    scope = SecretScope(
        category=PROVIDER_CREDENTIAL, provider_code="KIS", environment="PAPER"
    )
    base = secret_aad(logical_name="a", version=1, scope=scope, schema_version=1)

    assert base != secret_aad(
        logical_name="b", version=1, scope=scope, schema_version=1
    )
    assert base != secret_aad(
        logical_name="a", version=2, scope=scope, schema_version=1
    )
    assert base != secret_aad(
        logical_name="a",
        version=1,
        scope=SecretScope(
            category=PROVIDER_CREDENTIAL, provider_code="TOSS", environment="PAPER"
        ),
        schema_version=1,
    )
    assert base != secret_aad(
        logical_name="a",
        version=1,
        scope=SecretScope(
            category=PROVIDER_CREDENTIAL, provider_code="KIS", environment="LIVE"
        ),
        schema_version=1,
    )
    assert base != secret_aad(
        logical_name="a", version=1, scope=scope, schema_version=2
    )


def test_a_name_containing_the_separator_is_refused() -> None:
    # Otherwise two different scopes could produce the same AAD.
    with pytest.raises(ValueError, match="separator"):
        secret_aad(
            logical_name="a|1",
            version=1,
            scope=SecretScope(category=OAUTH, provider_code="GOOGLE", environment=None),
            schema_version=1,
        )


def test_an_oauth_secret_has_no_environment() -> None:
    with pytest.raises(ValueError, match="no environment"):
        SecretScope(category=OAUTH, provider_code="GOOGLE", environment="LIVE")


def test_a_provider_secret_must_name_its_environment() -> None:
    with pytest.raises(ValueError, match="PAPER or LIVE"):
        SecretScope(category=PROVIDER_CREDENTIAL, provider_code="KIS", environment=None)


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(ValueError, match="KIS, TOSS or BINANCE"):
        SecretScope(
            category=PROVIDER_CREDENTIAL, provider_code="ETRADE", environment="LIVE"
        )
