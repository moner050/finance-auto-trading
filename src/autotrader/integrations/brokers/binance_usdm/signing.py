from __future__ import annotations

import hashlib
import hmac
from collections.abc import Sequence
from urllib.parse import quote, urlencode

from pydantic import SecretStr


def sign_query(
    parameters: Sequence[tuple[str, str]],
    secret: SecretStr,
) -> str:
    encoded = encode_query(parameters)
    if type(secret) is not SecretStr:
        raise TypeError("Binance USD-M signing secret must be SecretStr")
    value = secret.get_secret_value()
    if not _single_line(value):
        del value
        raise ValueError("Binance USD-M signing secret is invalid")
    digest = hmac.new(
        value.encode("utf-8"),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    del value
    return digest


def encode_query(parameters: Sequence[tuple[str, str]]) -> str:
    if not parameters:
        raise ValueError("Binance USD-M signing parameters are invalid")
    normalized: list[tuple[str, str]] = []
    keys: set[str] = set()
    for item in parameters:
        if type(item) is not tuple or len(item) != 2:
            raise TypeError("Binance USD-M signing parameters are invalid")
        key, value = item
        if (
            type(key) is not str
            or type(value) is not str
            or not _single_line(key)
            or not _single_line(value)
            or not key.isascii()
            or key in keys
        ):
            raise ValueError("Binance USD-M signing parameters are invalid")
        keys.add(key)
        normalized.append((key, value))
    return urlencode(normalized, quote_via=quote, safe="~")


def _single_line(value: str) -> bool:
    return bool(value.strip()) and "\r" not in value and "\n" not in value


__all__ = ("encode_query", "sign_query")
