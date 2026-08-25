from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import NoReturn, cast

from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)

_INCOMPLETE = "Toss KR market calendar is incomplete"
_INVALID_TOKEN = "Toss KR market calendar access token is invalid"
_INVALID_DATE = "Toss KR market calendar date is invalid"
_KST_OFFSET = timedelta(hours=9)


class TossIncompleteKrMarketCalendar(RuntimeError):
    """Raised when the Toss KR calendar cannot form complete evidence."""


@dataclass(frozen=True, slots=True)
class TossKrSinglePriceAuctionWindow:
    """One documented Toss Korean integrated-market auction window in UTC."""

    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        if not _is_exact_utc(self.start_at) or not _is_exact_utc(self.end_at):
            raise ValueError("Toss KR auction window must use exact UTC")
        if self.end_at <= self.start_at:
            raise ValueError("Toss KR auction window must be positive")


@dataclass(frozen=True, slots=True)
class TossKrMarketCalendarDay:
    """One Toss calendar day and its explicit single-price windows."""

    calendar_date: date
    windows: tuple[TossKrSinglePriceAuctionWindow, ...]

    def __post_init__(self) -> None:
        if type(self.calendar_date) is not date:
            raise ValueError("Toss KR calendar date is invalid")
        values = cast(object, self.windows)
        if not isinstance(values, tuple) or not all(
            type(window) is TossKrSinglePriceAuctionWindow for window in self.windows
        ):
            raise ValueError("Toss KR calendar windows must be immutable")


@dataclass(frozen=True, slots=True)
class TossKrMarketCalendar:
    """The canonical previous, current, and next Toss Korean calendar days."""

    previous_business_day: TossKrMarketCalendarDay
    today: TossKrMarketCalendarDay
    next_business_day: TossKrMarketCalendarDay

    def __post_init__(self) -> None:
        days = (
            self.previous_business_day,
            self.today,
            self.next_business_day,
        )
        if not all(type(day) is TossKrMarketCalendarDay for day in days):
            raise ValueError("Toss KR calendar days are invalid")
        if not (
            self.previous_business_day.calendar_date
            < self.today.calendar_date
            < self.next_business_day.calendar_date
        ):
            raise ValueError("Toss KR calendar days must be ascending")


class TossKrMarketCalendarReadOnlyAdapter:
    """Reads Toss Korean calendar evidence without account or write capability."""

    def __init__(self, *, transport: AsyncHttpTransport) -> None:
        self._transport = transport

    async def read_kr_market_calendar(
        self, *, access_token: object, calendar_date: object = None
    ) -> TossKrMarketCalendar:
        transport = self._transport
        try:
            outcome = await _read_calendar(
                transport=transport,
                access_token=access_token,
                calendar_date=calendar_date,
            )
        finally:
            del self, transport, access_token, calendar_date
        validation_error, calendar = outcome
        del outcome
        if validation_error is not None:
            del calendar
            raise ValueError(validation_error)
        if calendar is None:
            raise TossIncompleteKrMarketCalendar(_INCOMPLETE)
        return calendar

    async def submit(self, *, command: object) -> NoReturn:
        del self, command
        raise BrokerWriteDisabled(
            "Toss KR market calendar write adapter is not enabled"
        )

    async def cancel(self, *, command: object) -> NoReturn:
        del self, command
        raise BrokerWriteDisabled(
            "Toss KR market calendar write adapter is not enabled"
        )

    async def replace(self, *, command: object) -> NoReturn:
        del self, command
        raise BrokerWriteDisabled(
            "Toss KR market calendar write adapter is not enabled"
        )


def is_toss_kr_single_price_auction(*, calendar: object, observed_at: object) -> bool:
    """Return membership in a documented UTC Toss auction interval only."""

    if type(calendar) is not TossKrMarketCalendar:
        raise ValueError("Toss KR calendar is invalid")
    if not _is_exact_utc(observed_at):
        raise ValueError("Toss KR observation must use exact UTC")
    resolved_calendar = calendar
    observed = cast(datetime, observed_at)
    return any(
        window.start_at <= observed < window.end_at
        for day in (
            resolved_calendar.previous_business_day,
            resolved_calendar.today,
            resolved_calendar.next_business_day,
        )
        for window in day.windows
    )


async def _read_calendar(
    *, transport: AsyncHttpTransport, access_token: object, calendar_date: object
) -> tuple[str | None, TossKrMarketCalendar | None]:
    request: BrokerRequest | None = None
    response: BrokerResponse | None = None
    try:
        validation_error = _validation_error(
            access_token=access_token, calendar_date=calendar_date
        )
        if validation_error is not None:
            return validation_error, None
        request = BrokerRequest(
            method="GET",
            path=_calendar_path(cast(date | None, calendar_date)),
            headers=(("Authorization", f"Bearer {access_token}"),),
        )
        response = await transport.request(request)
        return None, _decode_calendar(
            response, requested_date=cast(date | None, calendar_date)
        )
    except Exception:
        return None, None
    finally:
        del transport, access_token, calendar_date, request, response


def _validation_error(*, access_token: object, calendar_date: object) -> str | None:
    if not _single_line_text(access_token):
        return _INVALID_TOKEN
    if calendar_date is not None and type(calendar_date) is not date:
        return _INVALID_DATE
    return None


def _calendar_path(calendar_date: date | None) -> str:
    base = "/api/v1/market-calendar/KR"
    return base if calendar_date is None else f"{base}?date={calendar_date.isoformat()}"


def _decode_calendar(
    response: BrokerResponse, *, requested_date: date | None
) -> TossKrMarketCalendar | None:
    status = response.status
    body = response.body
    del response
    try:
        if status != 200:
            return None
        payload: object = json.loads(body)
        if not isinstance(payload, Mapping):
            return None
        payload_mapping = cast(Mapping[str, object], payload)
        result = payload_mapping.get("result")
        if not isinstance(result, Mapping):
            return None
        result_mapping = cast(Mapping[str, object], result)
        previous_business_day = _day_from_value(
            result_mapping.get("previousBusinessDay")
        )
        today = _day_from_value(result_mapping.get("today"))
        next_business_day = _day_from_value(result_mapping.get("nextBusinessDay"))
        if requested_date is not None and today.calendar_date != requested_date:
            raise ValueError
        return TossKrMarketCalendar(
            previous_business_day=previous_business_day,
            today=today,
            next_business_day=next_business_day,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        return None
    finally:
        del body


def _day_from_value(value: object) -> TossKrMarketCalendarDay:
    if not isinstance(value, Mapping):
        raise ValueError
    record = cast(Mapping[str, object], value)
    calendar_date = _calendar_date(record.get("date"))
    integrated = record.get("integrated")
    if integrated is None:
        return TossKrMarketCalendarDay(calendar_date=calendar_date, windows=())
    if not isinstance(integrated, Mapping):
        raise ValueError
    sessions = cast(Mapping[str, object], integrated)
    windows = tuple(
        window
        for window in (
            _pre_or_regular_window(
                sessions.get("preMarket"), calendar_date=calendar_date
            ),
            _pre_or_regular_window(
                sessions.get("regularMarket"), calendar_date=calendar_date
            ),
            _after_market_window(
                sessions.get("afterMarket"), calendar_date=calendar_date
            ),
        )
        if window is not None
    )
    return TossKrMarketCalendarDay(calendar_date=calendar_date, windows=windows)


def _pre_or_regular_window(
    value: object, *, calendar_date: date
) -> TossKrSinglePriceAuctionWindow | None:
    times = _session_times(
        value,
        calendar_date=calendar_date,
        auction_field="singlePriceAuctionStartTime",
    )
    if times is None:
        return None
    _, auction, end = times
    return TossKrSinglePriceAuctionWindow(start_at=auction, end_at=end)


def _after_market_window(
    value: object, *, calendar_date: date
) -> TossKrSinglePriceAuctionWindow | None:
    times = _session_times(
        value,
        calendar_date=calendar_date,
        auction_field="singlePriceAuctionEndTime",
    )
    if times is None:
        return None
    start, auction, _ = times
    return TossKrSinglePriceAuctionWindow(start_at=start, end_at=auction)


def _session_times(
    value: object, *, calendar_date: date, auction_field: str
) -> tuple[datetime, datetime, datetime] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError
    session = cast(Mapping[str, object], value)
    if any(name not in session for name in ("startTime", "endTime")):
        raise ValueError
    start = _kst_datetime(session.get("startTime"), calendar_date=calendar_date)
    auction = _optional_kst_datetime(
        session.get(auction_field), calendar_date=calendar_date
    )
    end = _kst_datetime(session.get("endTime"), calendar_date=calendar_date)
    if not start < end:
        raise ValueError
    if auction is None:
        return None
    if not start < auction < end:
        raise ValueError
    return start, auction, end


def _calendar_date(value: object) -> date:
    if (
        not isinstance(value, str)
        or len(value) != 10
        or value[4] != "-"
        or value[7] != "-"
        or not (value[:4] + value[5:7] + value[8:]).isdigit()
    ):
        raise ValueError
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError
    return parsed


def _kst_datetime(value: object, *, calendar_date: date) -> datetime:
    if not isinstance(value, str) or not value.endswith("+09:00"):
        raise ValueError
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != _KST_OFFSET:
        raise ValueError
    if parsed.date() != calendar_date:
        raise ValueError
    return parsed.astimezone(UTC)


def _optional_kst_datetime(value: object, *, calendar_date: date) -> datetime | None:
    return None if value is None else _kst_datetime(value, calendar_date=calendar_date)


def _is_exact_utc(value: object) -> bool:
    if type(value) is not datetime:
        return False
    observed = value
    return observed.tzinfo is UTC and observed.utcoffset() == timedelta(0)


def _single_line_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "\n" not in value
        and "\r" not in value
    )
