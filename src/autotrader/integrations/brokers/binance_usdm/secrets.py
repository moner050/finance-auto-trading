from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from pydantic import SecretStr

from autotrader.config.account_secrets import AccountSecretResolutionError

BINANCE_USDM_LIVE_REFERENCE = "secret://dotenv/binance/usdm/live"
_API_KEY = "BINANCE_USDM_API_KEY"
_SECRET_KEY = "BINANCE_USDM_SECRET_KEY"


@dataclass(frozen=True, slots=True)
class BinanceUsdmSecret:
    api_key: SecretStr
    secret_key: SecretStr

    def __post_init__(self) -> None:
        if not _secret(self.api_key) or not _secret(self.secret_key):
            raise ValueError("Binance USD-M secret is invalid")


def binance_usdm_api_key_fingerprint(secret: BinanceUsdmSecret) -> bytes:
    """Return a domain-separated digest without exposing the resolved API key."""
    if type(secret) is not BinanceUsdmSecret:
        raise TypeError("exact Binance USD-M secret is required")
    secret.__post_init__()
    api_key = secret.api_key.get_secret_value()
    try:
        return sha256(b"BINANCE_USDM_API_KEY_V1\0" + api_key.encode("utf-8")).digest()
    finally:
        del api_key


def resolve_binance_usdm_secret(
    reference: str,
    env: Mapping[str, str],
) -> BinanceUsdmSecret:
    """Resolve only the exact approved trade/read USD-M key reference."""
    if reference != BINANCE_USDM_LIVE_REFERENCE:
        del reference, env
        raise AccountSecretResolutionError("account secret is unavailable") from None
    try:
        api_key = env.get(_API_KEY)
        secret_key = env.get(_SECRET_KEY)
    except Exception:
        api_key = None
        secret_key = None
    del reference, env
    if not _single_line(api_key) or not _single_line(secret_key):
        del api_key, secret_key
        raise AccountSecretResolutionError("account secret is unavailable") from None
    assert isinstance(api_key, str)
    assert isinstance(secret_key, str)
    return BinanceUsdmSecret(
        api_key=SecretStr(api_key),
        secret_key=SecretStr(secret_key),
    )


def _secret(value: object) -> str:
    if type(value) is not SecretStr:
        return ""
    raw = value.get_secret_value()
    return raw if _single_line(raw) else ""


def _single_line(value: object) -> bool:
    return (
        type(value) is str
        and bool(value.strip())
        and "\r" not in value
        and "\n" not in value
        and "\\r" not in value
        and "\\n" not in value
    )


__all__ = (
    "BINANCE_USDM_LIVE_REFERENCE",
    "BinanceUsdmSecret",
    "binance_usdm_api_key_fingerprint",
    "resolve_binance_usdm_secret",
)
