from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast

from autotrader.shared.decimal import require_decimal


class TossCandleInterval(StrEnum):
    ONE_MINUTE = "1m"
    ONE_DAY = "1d"


@dataclass(frozen=True, slots=True)
class TossCandleRecord:
    """One provider timestamped Toss OHLCV record, not a strategy bar."""

    timestamp: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal
    currency: str

    def __post_init__(self) -> None:
        if (
            type(self.timestamp) is not datetime
            or self.timestamp.tzinfo is None
            or self.timestamp.utcoffset() is None
        ):
            raise ValueError("Toss candle timestamp must be timezone-aware")
        for name in (
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "volume",
        ):
            value = require_decimal(getattr(self, name))
            if value < 0 or (name != "volume" and value == 0):
                raise ValueError("Toss candle prices and volume are invalid")
            object.__setattr__(self, name, value)
        if self.high_price < self.low_price or not (
            self.low_price <= self.open_price <= self.high_price
            and self.low_price <= self.close_price <= self.high_price
        ):
            raise ValueError("Toss candle price range is invalid")
        if not self.currency or "\n" in self.currency:
            raise ValueError("Toss candle currency is invalid")


@dataclass(frozen=True, slots=True)
class TossCandlePage:
    """A raw Toss candle page that preserves the provider pagination cursor."""

    records: tuple[TossCandleRecord, ...]
    next_before: str | None

    def __post_init__(self) -> None:
        records = cast(object, self.records)
        if not isinstance(records, tuple):
            raise ValueError("Toss candle records must be an immutable tuple")
        raw_records = cast(tuple[object, ...], records)
        if not all(isinstance(record, TossCandleRecord) for record in raw_records):
            raise ValueError("Toss candle records must be an immutable tuple")
        if self.next_before is not None:
            _provider_datetime(self.next_before, name="nextBefore")


def _provider_datetime(value: object, *, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Toss candle {name} is invalid")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Toss candle {name} is invalid") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Toss candle timestamp is invalid")
    return timestamp
