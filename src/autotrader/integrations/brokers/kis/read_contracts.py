from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KisReadCredentials:
    """Call-scoped KIS credentials; callers must not persist this object."""

    access_token: str
    app_key: str
    app_secret: str

    def __post_init__(self) -> None:
        for name in ("access_token", "app_key", "app_secret"):
            value = getattr(self, name)
            if not value or "\n" in value:
                raise ValueError(f"{name} must be a non-empty single line")
