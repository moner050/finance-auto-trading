from __future__ import annotations

import pytest

from autotrader.security.second_password import (
    hash_second_password,
    verify_second_password,
)

PASSWORD = "synthetic-second-password"


def test_hash_uses_argon2id_and_verifies_only_the_correct_password() -> None:
    verifier = hash_second_password(PASSWORD)

    assert verifier.startswith("$argon2id$")
    assert PASSWORD not in verifier
    assert verify_second_password(verifier, PASSWORD) is True
    assert verify_second_password(verifier, "incorrect-password") is False


@pytest.mark.parametrize(
    "verifier",
    [
        "not-an-argon2-verifier",
        "$argon2id$v=19$m=65536,t=3,p=4$broken",
        "$argon2i$v=19$m=65536,t=3,p=4$c2FsdA$ZGlnaWVzdA",
    ],
)
def test_verify_rejects_corrupted_or_non_argon2id_verifiers(verifier: str) -> None:
    assert verify_second_password(verifier, PASSWORD) is False


def test_password_validation_error_is_redacted() -> None:
    sensitive_input = b"synthetic-second-password"

    with pytest.raises(ValueError) as exc_info:
        hash_second_password(sensitive_input)  # type: ignore[arg-type]

    assert sensitive_input.decode() not in str(exc_info.value)


def test_successful_verification_does_not_replace_the_verifier() -> None:
    verifier = hash_second_password(PASSWORD)
    original = verifier

    assert verify_second_password(verifier, PASSWORD) is True
    assert verifier == original
