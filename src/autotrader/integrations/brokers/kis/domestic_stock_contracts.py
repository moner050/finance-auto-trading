from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal
from enum import StrEnum
from typing import cast

from autotrader.shared.decimal import require_decimal


class KisDomesticMarket(StrEnum):
    KRX = "J"
    NXT = "NX"
    INTEGRATED = "UN"


class KisDomesticPriceBasis(StrEnum):
    ADJUSTED = "0"
    ORIGINAL = "1"


@dataclass(frozen=True, slots=True)
class KisDomesticPriceRecord:
    """A current-price snapshot, never a completed strategy bar."""

    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    last_price: Decimal
    cumulative_volume: Decimal

    def __post_init__(self) -> None:
        for name in (
            "open_price",
            "high_price",
            "low_price",
            "last_price",
            "cumulative_volume",
        ):
            value = require_decimal(getattr(self, name))
            if value < 0 or (name != "cumulative_volume" and value == 0):
                raise ValueError("KIS domestic price fields are invalid")
            object.__setattr__(self, name, value)
        if self.high_price < self.low_price or not (
            self.low_price <= self.open_price <= self.high_price
            and self.low_price <= self.last_price <= self.high_price
        ):
            raise ValueError("KIS domestic price range is invalid")


@dataclass(frozen=True, slots=True)
class KisDomesticMinuteRecord:
    """A provider-local minute record, not a completed strategy bar."""

    trading_date: date
    trading_time: time
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        if type(self.trading_date) is not date or type(self.trading_time) is not time:
            raise ValueError("KIS domestic minute record requires local date and time")
        if self.trading_time.second != 0 or self.trading_time.microsecond != 0:
            raise ValueError("KIS domestic minute record must be minute aligned")
        for name in (
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "volume",
        ):
            value = require_decimal(getattr(self, name))
            if value < 0 or (name != "volume" and value == 0):
                raise ValueError("KIS domestic minute fields are invalid")
            object.__setattr__(self, name, value)
        if self.high_price < self.low_price or not (
            self.low_price <= self.open_price <= self.high_price
            and self.low_price <= self.close_price <= self.high_price
        ):
            raise ValueError("KIS domestic minute range is invalid")


@dataclass(frozen=True, slots=True)
class KisDomesticMinuteChartPage:
    records: tuple[KisDomesticMinuteRecord, ...]

    def __post_init__(self) -> None:
        records = cast(object, self.records)
        if not isinstance(records, tuple):
            raise ValueError("KIS domestic minute records must be an immutable tuple")
        raw_records = cast(tuple[object, ...], records)
        if not all(
            isinstance(record, KisDomesticMinuteRecord) for record in raw_records
        ):
            raise ValueError("KIS domestic minute records must be an immutable tuple")


@dataclass(frozen=True, slots=True)
class KisDomesticDailyRecord:
    trading_date: date
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        if type(self.trading_date) is not date:
            raise ValueError("KIS domestic daily record requires a local date")
        for name in (
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "volume",
        ):
            value = require_decimal(getattr(self, name))
            if value < 0 or (name != "volume" and value == 0):
                raise ValueError("KIS domestic daily fields are invalid")
            object.__setattr__(self, name, value)
        if self.high_price < self.low_price or not (
            self.low_price <= self.open_price <= self.high_price
            and self.low_price <= self.close_price <= self.high_price
        ):
            raise ValueError("KIS domestic daily range is invalid")


@dataclass(frozen=True, slots=True)
class KisDomesticDailyChartPage:
    records: tuple[KisDomesticDailyRecord, ...]

    def __post_init__(self) -> None:
        records = cast(object, self.records)
        if not isinstance(records, tuple):
            raise ValueError("KIS domestic daily records must be an immutable tuple")
        raw_records = cast(tuple[object, ...], records)
        if not all(
            isinstance(record, KisDomesticDailyRecord) for record in raw_records
        ):
            raise ValueError("KIS domestic daily records must be an immutable tuple")
