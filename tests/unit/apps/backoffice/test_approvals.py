"""The second password, its approval, and what an approval may be spent on."""

from __future__ import annotations

import pytest

from autotrader.apps.backoffice.bootstrap import (
    MINIMUM_LENGTH,
    BootstrapRefusedError,
    read_password,
    require_acceptable,
)
from autotrader.apps.backoffice.second_password import (
    ApprovalRequest,
    authority_digest,
)

DIGEST = authority_digest({"armed": False})
OTHER_DIGEST = authority_digest({"armed": True})


def _request(**changes: object) -> ApprovalRequest:
    values: dict[str, object] = {
        "session_id": "a-session",
        "operator_email": "operator@example.com",
        "action": "ARM",
        "target_type": "GLOBAL",
        "target_key": "ALL",
        "authority_digest": DIGEST,
    }
    values.update(changes)
    return ApprovalRequest(**values)  # type: ignore[arg-type]


def test_the_same_request_binds_to_the_same_value() -> None:
    assert _request().binding() == _request().binding()


@pytest.mark.parametrize(
    "change",
    (
        {"session_id": "another-session"},
        {"operator_email": "someone@example.com"},
        {"action": "CLEAR_HALT"},
        {"target_type": "ACCOUNT"},
        {"target_key": "SOMETHING_ELSE"},
        {"authority_digest": OTHER_DIGEST},
    ),
)
def test_changing_any_part_changes_what_the_approval_authorizes(
    change: dict[str, object],
) -> None:
    # An approval that survived any of these could be spent on something the
    # operator never looked at.
    assert _request(**change).binding() != _request().binding()


def test_an_approval_must_name_what_it_is_for() -> None:
    for field in ("session_id", "operator_email", "action", "target_type"):
        with pytest.raises(ValueError, match=field):
            _request(**{field: ""})


def test_a_digest_that_is_not_sha256_is_refused() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        _request(authority_digest=b"too short")


def test_the_digest_does_not_depend_on_key_order() -> None:
    assert authority_digest({"a": 1, "b": 2}) == authority_digest({"b": 2, "a": 1})


def test_a_short_password_is_refused() -> None:
    with pytest.raises(BootstrapRefusedError, match=str(MINIMUM_LENGTH)):
        require_acceptable("x" * (MINIMUM_LENGTH - 1))


def test_a_padded_password_is_refused() -> None:
    # Surrounding space is invisible on retyping.
    with pytest.raises(BootstrapRefusedError, match="padded"):
        require_acceptable(" " + "x" * MINIMUM_LENGTH)


def test_a_long_password_needs_no_symbols_or_capitals() -> None:
    phrase = "correct horse battery staple"

    assert require_acceptable(phrase) == phrase


def test_the_two_entries_must_match() -> None:
    entries = iter(["correct horse battery staple", "something else entirely"])

    with pytest.raises(BootstrapRefusedError, match="did not match"):
        read_password(lambda _: next(entries))


def test_a_matching_pair_is_accepted() -> None:
    phrase = "correct horse battery staple"

    assert read_password(lambda _: phrase) == phrase
