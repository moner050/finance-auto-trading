"""How a time is written on the screen.

Everything in this system stores and reasons in UTC, which is right: an
exchange session, a lease expiry and a decision all have to compare across
venues, and a local clock with a rule about when it jumps is a bad place to
keep any of them.

The screen is a different question. One person operates this, from Korea, and
"was the loop running last night" is a question about their night. So the
projections stay UTC and the templates print KST, and the conversion lives in
one filter rather than in each template - a screen where one panel converted
and the next did not would be worse than either choice made consistently.

Fixed +09:00 rather than a zone database. Korea has observed no daylight
saving since 1988, so the offset is the whole rule, and depending on tzdata
being installed to learn a constant is a dependency bought for nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")

# Enough to line up in a column and to tell two decisions apart. Seconds are
# kept because five-minute passes land within the same minute often enough.
DEFAULT_PATTERN = "%m-%d %H:%M:%S"
# For tables that reach back further than a day - a universe history or a
# promotion record - where dropping the year would make two Septembers look
# like the same one.
FULL_PATTERN = "%Y-%m-%d %H:%M:%S"
ABSENT = "-"


def in_kst(value: datetime | None, pattern: str = DEFAULT_PATTERN) -> str:
    """A stored moment as the operator's clock reads it.

    Naive input is read as UTC, which is what it is everywhere here: the
    column type attaches UTC on the way out, and the few datetimes built in
    Python are aware. Guessing local for a naive value would be the one
    mistake that shifts a time by nine hours without saying anything.
    """
    if value is None:
        return ABSENT
    if type(value) is not datetime:
        raise TypeError("only a datetime can be shown as a time")
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(KST).strftime(pattern)


__all__ = ("ABSENT", "DEFAULT_PATTERN", "FULL_PATTERN", "KST", "in_kst")
