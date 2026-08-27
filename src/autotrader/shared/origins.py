"""What counts as a public origin this system may be reached at.

One rule, because two copies of it disagreed: the settings refused plain HTTP
outright while the backoffice config allowed loopback, so a loopback
deployment was configurable in one place and rejected in the other.

Loopback is the one exception, and it is not a convenience. An OAuth redirect
carries an authorization code in the URL and a session cookie rides the same
origin, so both need a channel nobody can read. Over 127.0.0.1 there is no
hop to read them on, which is why the loopback carve-out exists in RFC 8252
and why Google accepts a loopback redirect URI at all.
"""

from __future__ import annotations

from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class InvalidOriginError(ValueError):
    """Raised when a URL is not an origin this system may be reached at."""


def is_loopback(url: str) -> bool:
    return (urlsplit(url).hostname or "") in LOOPBACK_HOSTS


def require_public_origin(url: str, *, name: str) -> str:
    """The URL, if it is a bare origin on a channel nobody can read.

    A path, a query, a fragment or embedded credentials all mean the caller
    meant something other than an origin, and guessing which part to keep is
    how a redirect URI stops matching the one registered with the provider.
    """
    if type(url) is not str or not url.strip():
        raise InvalidOriginError(f"{name} is required")
    if "\\" in url or any(character.isspace() for character in url):
        raise InvalidOriginError(f"{name} must be a bare origin")
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as error:
        raise InvalidOriginError(f"{name} must be a bare origin") from error
    if (
        parsed.hostname is None
        or parsed.netloc.endswith(":")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise InvalidOriginError(f"{name} must be a bare origin")
    if parsed.scheme == "https":
        return url
    if parsed.scheme == "http" and is_loopback(url):
        return url
    raise InvalidOriginError(f"{name} must be HTTPS, or HTTP on loopback")


__all__ = (
    "LOOPBACK_HOSTS",
    "InvalidOriginError",
    "is_loopback",
    "require_public_origin",
)
