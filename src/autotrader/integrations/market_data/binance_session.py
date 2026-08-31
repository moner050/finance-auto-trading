"""Where to put a session boundary on a venue that never closes.

The strategy is intraday. It stops accepting entries thirty minutes before the
close and requires a flat book ten minutes before it, and it does not allow a
position overnight. A perpetual futures venue has no close, so a day has to be
cut somewhere, and the author never faced this question — he traded index
futures, which close on their own.

The cut is placed by measurement rather than convention. Over sixty-two days of
hourly bars, BTCUSDT's quote volume by UTC hour:

    13:00  3.07x the median      20:00  0.66x   <- thinnest
    14:00  2.98x                 21:00  0.69x
    15:00  2.44x                 23:00  0.69x
    16:00  1.56x                 22:00  0.75x

Closing at 20:00 UTC puts the whole thin block outside the session, so no
position is held through it, and leaves the forced-flat window at 19:50 in the
19:00 hour, which trades at 0.93x the median. Flattening into thin liquidity
is how an exit that had to happen becomes an exit at a bad price.

The alternative was closing at 16:00, which would flatten into the deepest
hour of the day but would then run the session straight through the thin block
with a position open. Avoiding the drought was the instruction.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from autotrader.strategies.david_v6.sessions import ExchangeCalendar, SessionKind

# The hour the thin block starts, measured rather than chosen. Changing it
# changes when every position is forced flat, so it is stated once here.
SESSION_CLOSE_HOUR = 20
SOURCE_TIMEZONE = "UTC"

# A day's calendar is a fact about the clock, not about the market, so it does
# not go stale mid-session. It is valid until the session it describes ends.
_DAY = timedelta(days=1)


def binance_usdm_calendar(
    *, session_date: date, captured_at: datetime
) -> ExchangeCalendar:
    """The session that opens on `session_date` and closes a day later.

    A session runs 20:00 UTC to 20:00 UTC, so its date is the date it opened
    on. The strategy's own cutoffs then fall at 19:30 and 19:50 UTC the
    following day.
    """
    if type(session_date) is not date:
        raise TypeError("session_date must be an exact date")
    if captured_at.tzinfo is None:
        raise ValueError("captured_at must be timezone-aware")
    opened = datetime.combine(session_date, time(hour=SESSION_CLOSE_HOUR), tzinfo=UTC)
    closed = opened + _DAY
    return ExchangeCalendar(
        session_date=session_date,
        kind=SessionKind.BINANCE_USDM,
        source_timezone=SOURCE_TIMEZONE,
        # The venue does not take holidays. Reporting one would be inventing a
        # day the market was open on.
        is_trading_day=True,
        session_open_at=opened,
        session_close_at=closed,
        # A close auction is a KRX construct and a pre-open is an equity one.
        close_auction_at=None,
        pre_open_at=None,
        captured_at=captured_at.astimezone(UTC),
        valid_until=closed,
    )


def session_date_for(moment: datetime) -> date:
    """Which session a moment falls in.

    Before the close hour the moment still belongs to the session that opened
    the day before, which is the part a calendar keyed on the calendar date
    gets wrong.
    """
    if moment.tzinfo is None:
        raise ValueError("moment must be timezone-aware")
    utc = moment.astimezone(UTC)
    if utc.hour < SESSION_CLOSE_HOUR:
        return (utc - _DAY).date()
    return utc.date()


__all__ = (
    "SESSION_CLOSE_HOUR",
    "SOURCE_TIMEZONE",
    "binance_usdm_calendar",
    "session_date_for",
)
