"""One rule for what counts as an origin this system may be reached at.

Two copies of this disagreed once: the settings refused plain HTTP outright
while the backoffice config allowed loopback, so a loopback deployment was
configurable in one place and rejected in the other.
"""

from __future__ import annotations

import pytest

from autotrader.shared.origins import (
    InvalidOriginError,
    is_loopback,
    require_public_origin,
)


@pytest.mark.parametrize(
    "url",
    (
        "https://backoffice.example.com",
        "https://backoffice.example.com/",
        "https://backoffice.example.com:8443",
    ),
)
def test_an_https_origin_is_accepted(url: str) -> None:
    assert require_public_origin(url, name="url") == url


@pytest.mark.parametrize(
    "url",
    ("http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"),
)
def test_loopback_may_use_plain_http(url: str) -> None:
    # There is no hop between the browser and the server to read the
    # authorization code or the session cookie off.
    assert require_public_origin(url, name="url") == url
    assert is_loopback(url) is True


def test_plain_http_anywhere_else_is_refused() -> None:
    with pytest.raises(InvalidOriginError, match="HTTPS, or HTTP on loopback"):
        require_public_origin("http://backoffice.example.com", name="url")


def test_a_host_that_merely_contains_localhost_is_not_loopback() -> None:
    # "localhost.example.com" resolves on the public internet.
    assert is_loopback("http://localhost.example.com") is False
    with pytest.raises(InvalidOriginError):
        require_public_origin("http://localhost.example.com", name="url")


@pytest.mark.parametrize(
    "url",
    (
        "https://backoffice.example.com/dashboard",
        "https://backoffice.example.com?a=1",
        "https://backoffice.example.com#fragment",
        "https://user:pass@backoffice.example.com",
        "https://backoffice.example.com:",
        "https://backoffice.example.com" + chr(92) + "evil",
        "https://backoffice.example.com ",
        "ftp://backoffice.example.com",
        "backoffice.example.com",
        "",
        "   ",
    ),
)
def test_anything_that_is_not_a_bare_origin_is_refused(url: str) -> None:
    # A path or a query means the caller meant something else, and guessing
    # which part to keep is how a redirect URI stops matching the registered
    # one.
    with pytest.raises(InvalidOriginError):
        require_public_origin(url, name="url")


def test_the_error_names_what_was_being_read() -> None:
    with pytest.raises(InvalidOriginError, match="BACKOFFICE_PUBLIC_URL"):
        require_public_origin("", name="BACKOFFICE_PUBLIC_URL")
