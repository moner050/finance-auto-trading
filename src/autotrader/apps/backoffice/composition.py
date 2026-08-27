"""Build the backoffice from configuration, or refuse to build it.

The HTTPS transport lives here rather than beside the flow it serves, because
the flow is worth reading without a client library in the way, and because a
test that needs the network to check a signature is not checking a signature.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import UUID

import httpx
from fastapi import FastAPI
from redis import asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.app import create_app
from autotrader.apps.backoffice.auth import (
    BackofficeConfig,
    IdentityUnavailableError,
    normalize_email,
)
from autotrader.apps.backoffice.google import GoogleIdentityProvider
from autotrader.apps.backoffice.sessions import RedisSessionClient, RedisSessionStore
from autotrader.config.settings import Settings

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class HttpxTransport:
    """The two Google calls, over one client."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=_TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(self, url: str) -> Mapping[str, object]:
        return _payload(await self._client.get(url))

    async def post_form(
        self, url: str, form: Mapping[str, str]
    ) -> Mapping[str, object]:
        return _payload(await self._client.post(url, data=dict(form)))


def _payload(response: httpx.Response) -> Mapping[str, object]:
    # The body of a failed token exchange can carry the reason it failed,
    # which is a detail for a log and not for a caller, so only the status is
    # kept in the error.
    if response.status_code != 200:
        raise IdentityUnavailableError(
            f"{response.request.url.path} answered {response.status_code}"
        )
    body = response.json()
    if not isinstance(body, dict):
        raise IdentityUnavailableError("expected a JSON object")
    return cast("Mapping[str, object]", body)


def backoffice_config(settings: Settings) -> BackofficeConfig:
    """Read the configuration, raising with the name of what is missing.

    Nothing here defaults. A backoffice that starts with half its identity
    configuration is the one outcome this whole path exists to prevent.
    """
    public_url = settings.backoffice_public_url
    allowed_email = settings.backoffice_allowed_email
    client_id = settings.oauth_google_client_id
    client_secret = settings.oauth_google_client_secret
    redis_url = settings.redis_connection_url
    for name, value in (
        ("BACKOFFICE_PUBLIC_URL", public_url),
        ("BACKOFFICE_ALLOWED_EMAIL", allowed_email),
        ("OAUTH_GOOGLE_CLIENT_ID", client_id),
        ("OAUTH_GOOGLE_CLIENT_SECRET", client_secret),
        ("REDIS_HOST, REDIS_PORT and REDIS_PW", redis_url),
    ):
        if value is None:
            raise IdentityUnavailableError(f"{name} is required")
    assert public_url is not None
    assert allowed_email is not None
    assert client_id is not None
    assert client_secret is not None
    assert redis_url is not None
    return BackofficeConfig(
        public_url=str(public_url),
        allowed_email=normalize_email(allowed_email),
        client_id=client_id,
        client_secret=client_secret.get_secret_value(),
        redis_url=redis_url,
    )


def build_backoffice(
    *,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    account_id: UUID,
    transport: HttpxTransport | None = None,
) -> FastAPI:
    config = backoffice_config(settings)
    return create_app(
        config=config,
        sessions=sessions,
        store=RedisSessionStore(
            cast(
                RedisSessionClient,
                redis.from_url(config.redis_url, decode_responses=True),
            )
        ),
        provider=GoogleIdentityProvider(
            config=config, transport=transport or HttpxTransport()
        ),
        account_id=account_id,
    )


__all__ = ("HttpxTransport", "backoffice_config", "build_backoffice")
