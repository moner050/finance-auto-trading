from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from autotrader.shared.decimal import require_decimal


@dataclass(frozen=True, slots=True)
class CompletedOhlcvBar:
    """A provider-neutral completed OHLCV bar with the source open retained."""

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        if (
            type(self.timestamp) is not datetime
            or self.timestamp.tzinfo is not UTC
            or self.timestamp.utcoffset() != timedelta(0)
        ):
            raise ValueError("completed OHLCV timestamp must use exact UTC")
        for name in ("open", "high", "low", "close", "volume"):
            object.__setattr__(self, name, require_decimal(getattr(self, name)))
        if any(value <= 0 for value in (self.open, self.high, self.low, self.close)):
            raise ValueError("completed OHLCV prices must be positive")
        if self.high < self.low:
            raise ValueError("completed OHLCV high must be at least low")
        if not self.low <= self.open <= self.high:
            raise ValueError("completed OHLCV open must be within range")
        if not self.low <= self.close <= self.high:
            raise ValueError("completed OHLCV close must be within range")
        if self.volume < 0:
            raise ValueError("completed OHLCV volume must be non-negative")
