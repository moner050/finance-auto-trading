"""What the register form is allowed to offer.

The name used to be typed. A name no adapter looks for stores perfectly well
and is never read, so the secret is registered, activated, and silently absent
at the moment something needs it.
"""

from __future__ import annotations

import pytest

from autotrader.apps.backoffice.provider_secrets import (
    fields_for,
    registerable_for,
    registerable_secrets,
)
from autotrader.apps.backoffice.secrets import (
    ACCOUNT_IDENTIFIER,
    OAUTH,
    PROVIDER_CREDENTIAL,
)


def test_every_provider_field_is_offered() -> None:
    """Derived from `fields_for` rather than listed, so a field added to a
    provider appears on the form without anyone remembering to add it."""
    offered = {entry.logical_name for entry in registerable_secrets()}
    for provider, environment in (
        ("KIS", "LIVE"),
        ("KIS", "PAPER"),
        ("TOSS", "LIVE"),
        ("BINANCE", "LIVE"),
        ("BINANCE", "PAPER"),
    ):
        for field in fields_for(provider, environment):
            assert field.logical_name in offered


def test_the_name_matches_what_the_resolver_looks_for() -> None:
    """`ProviderField` builds it from the scope so it cannot be typed
    differently twice. The form reads the same source."""
    for entry in registerable_secrets():
        if entry.environment is None:
            continue
        expected = {
            field.logical_name
            for field in fields_for(entry.provider_code, entry.environment)
        }
        assert entry.logical_name in expected


def test_the_category_follows_the_field_not_the_operator() -> None:
    """An account number is an identifier and an app key is a credential.
    Asking which would be asking the operator to know something the field
    already says."""
    by_name = {entry.logical_name: entry for entry in registerable_secrets()}

    assert by_name["kis-live-account-number"].category == ACCOUNT_IDENTIFIER
    assert by_name["kis-live-product-code"].category == ACCOUNT_IDENTIFIER
    assert by_name["kis-live-app-key"].category == PROVIDER_CREDENTIAL
    assert by_name["binance-live-secret-key"].category == PROVIDER_CREDENTIAL
    assert by_name["google-oauth-client-secret"].category == OAUTH


def test_google_carries_no_environment() -> None:
    """`SecretScope` refuses an OAUTH secret that has one, so offering one
    would produce a slot that cannot be saved."""
    google = [
        entry for entry in registerable_secrets() if entry.provider_code == "GOOGLE"
    ]

    assert google
    assert all(entry.environment is None for entry in google)


def test_a_slot_that_is_not_offered_is_refused() -> None:
    """Matched against the catalogue rather than parsed. A slot assembled by
    hand would otherwise register a secret under a name nothing reads."""
    with pytest.raises(ValueError, match="등록할 수 있는"):
        registerable_for("BINANCE:LIVE:whatever-i-like")


def test_every_slot_resolves_to_itself() -> None:
    for entry in registerable_secrets():
        assert registerable_for(entry.slot) is not None


def test_no_two_slots_share_an_identifier() -> None:
    """The form posts the slot. Two entries sharing one would register the
    wrong secret for whichever the operator picked second."""
    slots = [entry.slot for entry in registerable_secrets()]

    assert len(slots) == len(set(slots))
