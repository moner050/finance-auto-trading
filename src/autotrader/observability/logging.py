from __future__ import annotations

from collections.abc import Mapping
from typing import cast

SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "token",
        "secret",
        "secret_key",
        "password",
        "account_number",
        "broker_account_ref",
    }
)


def redact_sensitive_values(value: object) -> object:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        if all(isinstance(key, str) for key in mapping):
            string_mapping = cast(Mapping[str, object], mapping)
            return {
                key: "[REDACTED]"
                if key.casefold() in SENSITIVE_KEYS
                else redact_sensitive_values(item)
                for key, item in string_mapping.items()
            }
        return cast(object, value)
    if isinstance(value, list):
        return [redact_sensitive_values(item) for item in cast(list[object], value)]
    if isinstance(value, tuple):
        return tuple(
            redact_sensitive_values(item) for item in cast(tuple[object, ...], value)
        )
    return value
