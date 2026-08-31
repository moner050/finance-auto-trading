"""Cutting a day on a venue that never closes.

The strategy forces a flat book before the close, so wherever the close is put
becomes the moment every position is liquidated. These pin where that falls and
what it costs, because the answer came from measurement and a later edit should
have to argue with it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from autotrader.integrations.market_data.binance_session import (
    SESSION_CLOSE_HOUR,
    binance_usdm_calendar,
    session_date_for,
)
from autotrader.strategies.david_v6.sessions import SessionKind, evaluate_session

DAY = date(2026, 8, 27)
CAPTURED = datetime(2026, 8, 27, 20, 0, tzinfo=UTC)


def _calendar() -> object:
    return binance_usdm_calendar(session_date=DAY, captured_at=CAPTURED)


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=UTC)


def test_the_close_is_where_liquidity_thins() -> None:
    """Measured over sixty-two days: 20:00 to 23:00 UTC trades at two thirds
    of the median, and 20:00 is the thinnest hour of the day."""
    assert SESSION_CLOSE_HOUR == 20


def test_a_session_runs_a_full_day_from_the_close_hour() -> None:
    calendar = _calendar()

    assert calendar.session_open_at == _at(27, 20)  # type: ignore[attr-defined]
    assert calendar.session_close_at == _at(28, 20)  # type: ignore[attr-defined]
    assert calendar.kind is SessionKind.BINANCE_USDM  # type: ignore[attr-defined]


def test_the_venue_takes_no_holidays() -> None:
    """Reporting one would invent a day the market was open on."""
    assert _calendar().is_trading_day is True  # type: ignore[attr-defined]


def test_a_close_auction_belongs_to_another_venue() -> None:
    calendar = _calendar()

    assert calendar.close_auction_at is None  # type: ignore[attr-defined]
    assert calendar.pre_open_at is None  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("day", "hour", "minute", "entry_allowed", "must_be_flat"),
    (
        # Just after the open, and through the deepest hours of the day.
        (27, 21, 0, True, False),
        (28, 13, 0, True, False),
        (28, 19, 20, True, False),
        # Thirty minutes before the close, entries stop.
        (28, 19, 40, False, False),
        # Ten minutes before it, the book has to be flat.
        (28, 19, 55, False, True),
    ),
)
def test_the_cutoffs_fall_where_the_strategy_puts_them(
    day: int, hour: int, minute: int, entry_allowed: bool, must_be_flat: bool
) -> None:
    facts = evaluate_session(_calendar(), _at(day, hour, minute))  # type: ignore[arg-type]

    assert facts.entry_allowed is entry_allowed
    assert facts.must_be_flat is must_be_flat


def test_the_flat_window_is_not_in_the_thin_block() -> None:
    """Flattening into thin liquidity is how an exit that had to happen
    becomes an exit at a bad price. The window sits in the 19:00 hour, which
    trades near the median; the thin block starts as the session ends."""
    facts = evaluate_session(_calendar(), _at(28, 19, 55))  # type: ignore[arg-type]

    assert facts.flat_at == _at(28, 19, 50)
    assert facts.flat_at.hour < SESSION_CLOSE_HOUR  # type: ignore[union-attr]


def test_a_moment_before_the_close_hour_belongs_to_the_previous_session() -> None:
    """Keying a calendar on the calendar date would put five in the morning in
    the session that has not opened yet."""
    assert session_date_for(_at(28, 5)) == date(2026, 8, 27)
    assert session_date_for(_at(28, 19, 59)) == date(2026, 8, 27)
    assert session_date_for(_at(28, 20)) == date(2026, 8, 28)


def test_a_naive_moment_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        session_date_for(datetime(2026, 8, 28, 5, 0))


def test_a_naive_capture_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        binance_usdm_calendar(
            session_date=DAY, captured_at=datetime(2026, 8, 27, 20, 0)
        )
