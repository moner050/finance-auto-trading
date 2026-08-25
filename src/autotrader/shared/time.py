from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware UTC datetime is required")
    return value.astimezone(UTC)


class Clock(Protocol):
    def now(self) -> datetime: ...
