"""Refusing a public URL the browser will not find the process at.

The Google flow was configured correctly in every part anybody looks at - the
client, the scopes, PKCE, the allowed email - and would still have failed at
the last step, because the public URL named one port and the process bound
another. The identity provider sends the operator back to the published port,
and nothing was listening there.

Behind a reverse proxy the two differ on purpose, so the rule applies only
where the public URL is loopback, which is what says there is no proxy.
"""

from __future__ import annotations

from base64 import b64encode

import pytest

from autotrader.apps.backoffice.__main__ import require_reachable_public_url
from autotrader.config.settings import Settings


def _settings(*, public_url: str, bind_port: int) -> Settings:
    return Settings(
        backoffice_public_url=public_url,  # type: ignore[arg-type]
        backoffice_bind_port=bind_port,
        backoffice_master_key=b64encode(b"k" * 32).decode(),  # type: ignore[arg-type]
        backoffice_master_key_version=1,
    )


def test_a_loopback_url_must_name_the_port_the_process_binds() -> None:
    with pytest.raises(SystemExit, match="callback would reach nothing"):
        require_reachable_public_url(
            _settings(public_url="http://127.0.0.1:6086", bind_port=8000)
        )


def test_matching_ports_are_accepted() -> None:
    require_reachable_public_url(
        _settings(public_url="http://127.0.0.1:6086", bind_port=6086)
    )


def test_a_proxied_deployment_may_publish_a_different_port() -> None:
    """Caddy publishes 443 and the app binds 8000 on a network only the proxy
    can reach. That is the compose topology, not a misconfiguration."""
    require_reachable_public_url(
        _settings(public_url="https://backoffice.example.com", bind_port=8000)
    )


def test_a_bare_loopback_url_is_read_as_its_default_port() -> None:
    with pytest.raises(SystemExit):
        require_reachable_public_url(
            _settings(public_url="http://localhost", bind_port=8000)
        )
