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
from unittest.mock import patch
from uuid import UUID, uuid7

import pytest

from autotrader.apps.backoffice.__main__ import (
    prepare,
    require_account_id,
    require_reachable_public_url,
)
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


class _RecordingEngine:
    """Stands in for the engine, so disposal can be observed."""

    def __init__(self) -> None:
        self.disposed = 0

    async def dispose(self) -> None:
        self.disposed += 1


@pytest.mark.asyncio
async def test_preparing_leaves_no_connection_on_the_building_loop() -> None:
    """Building reads the configuration out of MySQL, which fills the pool
    with connections belonging to whatever loop opened them. `asyncio.run`
    then closes that loop and uvicorn starts its own, so the first request
    reaches for a connection whose transport is gone.

    It surfaces as `AttributeError: 'NoneType' object has no attribute 'send'`
    from deep inside asyncio - a database failure that names no database, on
    the operations page, immediately after a sign-in that worked. Which is
    exactly how it was found.
    """
    engine = _RecordingEngine()

    async def _build(**_: object) -> object:
        return object()

    with patch("autotrader.apps.backoffice.__main__.build_backoffice", _build):
        await prepare(
            settings=_settings(public_url="http://127.0.0.1:6086", bind_port=6086),
            engine=engine,  # type: ignore[arg-type]
            account_id=UUID(int=0),
        )

    assert engine.disposed == 1


@pytest.mark.asyncio
async def test_a_failed_build_still_leaves_nothing_open() -> None:
    """A half-built process must not leave a connection behind for a loop
    that will never own it."""
    engine = _RecordingEngine()

    async def _fail(**_: object) -> object:
        raise RuntimeError("configuration unavailable")

    with (
        patch("autotrader.apps.backoffice.__main__.build_backoffice", _fail),
        pytest.raises(RuntimeError, match="configuration unavailable"),
    ):
        await prepare(
            settings=_settings(public_url="http://127.0.0.1:6086", bind_port=6086),
            engine=engine,  # type: ignore[arg-type]
            account_id=UUID(int=0),
        )

    assert engine.disposed == 1


def test_a_non_v7_account_id_is_refused_before_the_server_starts() -> None:
    """Every id in this schema is a UUIDv7 and the column type enforces it, so
    the nil UUID reached the database and was refused there - as a bind error
    while rendering a page, which surfaces as a 500 naming a SELECT rather
    than the argument that was wrong."""
    with pytest.raises(SystemExit, match="not a UUIDv7"):
        require_account_id("00000000-0000-0000-0000-000000000000")


def test_text_that_is_not_a_uuid_is_refused() -> None:
    with pytest.raises(SystemExit, match="not a UUID"):
        require_account_id("the-account")


def test_a_v7_is_accepted() -> None:
    generated = uuid7()

    assert require_account_id(str(generated)) == generated
