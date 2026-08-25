from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerRequest,
)


class KisAuthenticationError(RuntimeError):
    """Raised when KIS does not return a valid OAuth access token."""


@dataclass(frozen=True, slots=True)
class KisClientCredentials:
    app_key: str
    app_secret: str

    def __post_init__(self) -> None:
        if any(not value or "\n" in value for value in (self.app_key, self.app_secret)):
            raise ValueError("KIS credentials must be non-empty single lines")


@dataclass(frozen=True, slots=True)
class KisAccessToken:
    value: str
    expires_at_raw: str

    def __post_init__(self) -> None:
        if any(
            not value or "\n" in value for value in (self.value, self.expires_at_raw)
        ):
            raise ValueError("KIS access token is invalid")
        try:
            parsed = datetime.strptime(self.expires_at_raw, "%Y-%m-%d %H:%M:%S")
        except ValueError as error:
            raise ValueError("KIS token expiry is invalid") from error
        if parsed.strftime("%Y-%m-%d %H:%M:%S") != self.expires_at_raw:
            raise ValueError("KIS token expiry is invalid")


async def issue_kis_access_token(
    *, transport: AsyncHttpTransport, credentials: KisClientCredentials
) -> KisAccessToken:
    response = await transport.request(
        BrokerRequest(
            method="POST",
            path="/oauth2/tokenP",
            headers=(
                ("Content-Type", "application/json; charset=UTF-8"),
                ("Accept", "text/plain"),
            ),
            body=json.dumps(
                {
                    "grant_type": "client_credentials",
                    "appkey": credentials.app_key,
                    "appsecret": credentials.app_secret,
                },
                separators=(",", ":"),
            ).encode("utf-8"),
        )
    )
    if response.status != 200:
        raise KisAuthenticationError("KIS OAuth request failed")
    try:
        payload: object = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KisAuthenticationError("KIS OAuth response is invalid") from error
    if not isinstance(payload, dict):
        raise KisAuthenticationError("KIS OAuth response is invalid")
    values = cast(dict[str, object], payload)
    token = values.get("access_token")
    expiry = values.get("access_token_token_expired")
    if not isinstance(token, str) or not isinstance(expiry, str):
        raise KisAuthenticationError("KIS OAuth response is invalid")
    try:
        return KisAccessToken(value=token, expires_at_raw=expiry)
    except ValueError as error:
        raise KisAuthenticationError("KIS OAuth response is invalid") from error
