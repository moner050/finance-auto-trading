from __future__ import annotations

import gc

import pytest

from autotrader.application.backoffice_security import SecretReference, SecretValue


def test_secret_reference_parses_only_exact_active_database_reference() -> None:
    reference = SecretReference.parse("secret://db/kis-live-credentials@active")

    assert reference.logical_name == "kis-live-credentials"
    assert str(reference) == "secret://db/kis-live-credentials@active"


@pytest.mark.parametrize(
    "value",
    [
        "secret://db/Kis-live@active",
        "secret://db/kis_live@active",
        "secret://db/-kis-live@active",
        "secret://db/kis--live@active",
        "secret://db/kis-live-@active",
        "secret://db/kis-live@latest",
        "secret://database/kis-live@active",
        "secret://db/kis-live@active?version=1",
        " secret://db/kis-live@active",
    ],
)
def test_secret_reference_rejects_aliases_and_non_kebab_names(value: str) -> None:
    with pytest.raises(ValueError, match="invalid"):
        SecretReference.parse(value)


def test_secret_reference_constructor_enforces_logical_name_contract() -> None:
    with pytest.raises(ValueError, match="invalid"):
        SecretReference(logical_name="not_valid")


def test_secret_value_exposes_plaintext_only_through_explicit_accessor() -> None:
    value = SecretValue("synthetic-sensitive-value")

    assert value.get_secret_value() == "synthetic-sensitive-value"
    assert repr(value) == "SecretValue('[REDACTED]')"
    assert str(value) == "SecretValue('[REDACTED]')"
    assert "synthetic-sensitive-value" not in repr(value)
    assert "synthetic-sensitive-value" not in str(value)
    assert not hasattr(value, "__dict__")


def test_secret_value_does_not_retain_plaintext_between_accessor_calls() -> None:
    plaintext = "synthetic-sensitive-value"
    plaintext_bytes = plaintext.encode("utf-8")
    value = SecretValue(plaintext)

    assert value.get_secret_value() == plaintext
    assert value.get_secret_value() == plaintext

    referents = gc.get_referents(value)
    assert plaintext not in referents
    assert plaintext_bytes not in referents


def test_secret_value_validation_error_does_not_include_input() -> None:
    sensitive_input = b"synthetic-sensitive-value"

    with pytest.raises(ValueError) as exc_info:
        SecretValue(sensitive_input)  # type: ignore[arg-type]

    assert sensitive_input.decode() not in str(exc_info.value)
