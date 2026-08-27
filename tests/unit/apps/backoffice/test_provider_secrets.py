"""Naming a provider credential, and collecting a whole set of them."""

from __future__ import annotations

import pytest

from autotrader.apps.backoffice.credentials import (
    CredentialsRefusedError,
    main,
    read_values,
)
from autotrader.apps.backoffice.provider_secrets import (
    BINANCE,
    KIS,
    LIVE,
    PAPER,
    TOSS,
    ProviderField,
    fields_for,
)
from autotrader.apps.backoffice.secrets import (
    ACCOUNT_IDENTIFIER,
    PROVIDER_CREDENTIAL,
)


def test_a_name_is_built_from_its_scope() -> None:
    # Never typed twice, so it cannot be typed differently twice.
    field = ProviderField(provider=KIS, environment=PAPER, field="app-key")

    assert field.logical_name == "kis-paper-app-key"
    assert field.reference == "secret://db/kis-paper-app-key@active"


def test_the_same_field_in_two_environments_is_two_secrets() -> None:
    live = ProviderField(provider=KIS, environment=LIVE, field="app-key")
    paper = ProviderField(provider=KIS, environment=PAPER, field="app-key")

    assert live.logical_name != paper.logical_name


def test_an_account_number_is_an_identifier_rather_than_a_credential() -> None:
    # The schema separates the two, and so does what may be shown about them.
    assert (
        ProviderField(
            provider=KIS, environment=LIVE, field="account-number"
        ).scope.category
        == ACCOUNT_IDENTIFIER
    )
    assert (
        ProviderField(provider=KIS, environment=LIVE, field="app-key").scope.category
        == PROVIDER_CREDENTIAL
    )


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        ProviderField(provider="ETRADE", environment=LIVE, field="app-key")


def test_an_unknown_environment_is_refused() -> None:
    with pytest.raises(ValueError, match="LIVE or PAPER"):
        ProviderField(provider=KIS, environment="SHADOW", field="app-key")


def test_every_provider_declares_its_whole_set() -> None:
    assert len(fields_for(KIS, PAPER)) == 4
    assert len(fields_for(TOSS, LIVE)) == 2
    assert len(fields_for(BINANCE, LIVE)) == 2


def test_a_whole_set_is_collected_before_anything_is_stored() -> None:
    fields = fields_for(TOSS, LIVE)
    entries = iter(["an-id", "a-secret"])

    values = read_values(fields, lambda _: next(entries))

    assert values == {"client-id": "an-id", "client-secret": "a-secret"}


def test_a_blank_value_stops_the_whole_set() -> None:
    # Three of four values is an account that reads as unconfigured.
    fields = fields_for(TOSS, LIVE)
    entries = iter(["an-id", "   "])

    with pytest.raises(CredentialsRefusedError, match="client-secret"):
        read_values(fields, lambda _: next(entries))


def test_a_value_spanning_lines_is_refused_here_rather_than_at_signing() -> None:
    fields = fields_for(TOSS, LIVE)
    entries = iter(["an-id", "two\nlines"])

    with pytest.raises(CredentialsRefusedError, match="single line"):
        read_values(fields, lambda _: next(entries))


@pytest.mark.parametrize(
    "argv",
    ((), ("KIS",), ("KIS", "SHADOW"), ("ETRADE", "LIVE"), ("KIS", "LIVE", "extra")),
)
def test_the_command_refuses_anything_but_a_provider_and_an_environment(
    argv: tuple[str, ...],
) -> None:
    assert main(argv) == 2
