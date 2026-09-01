"""The economic calendar, which nothing was producing.

Section 8's news filter is A0 - confirmed from the author - and section 21's
prohibited list names `trading_during_2_3_star_news` outright. The rule was
implemented: `evaluate_event_window` blocks two- and three-star releases for
ten minutes when there was no surprise, two hours when there was or when it is
unknown, and the whole session for the monthly employment report. Nothing ever
handed it a calendar, so `inputs.events` was always None, every pass recorded
CALENDAR_UNAVAILABLE, and `engine.py` turns any blocker into a REJECT. The
guard was not merely off; it made a tradeable decision impossible.

The source is ForexFactory's weekly feed. Its Low/Medium/High rating is the
one-two-three star scale section 8 is written against, which is why that
mapping is a translation rather than a judgement. There is no key and no
account, and only `thisweek` exists - `nextweek` answers 404 - which is what
bounds the validity below.

Three things the feed does not say, and what is done about each.

It carries no `actual`, only `forecast` and `previous`. Whether a release
surprised is knowable only after it lands, so `strong_surprise` is None on
every event, which routes to the two-hour window rather than the ten-minute
one. Unknown is treated as strong, which is the safe direction.

It gives no identity. One is built from country, title and instant, which is
what actually distinguishes two rows - three separate RBNZ events share a
timestamp and differ only by title.

Its `Holiday` rows carry no impact rating at all. They are dropped rather than
assigned one: a holiday is not a release, and inventing a star for it would be
putting a number the source never gave into evidence. Nothing is lost, because
only two stars and above can block.

The identity of the employment report is exact rather than substring. The feed
carries both `Non-Farm Employment Change` and `ADP Non-Farm Employment
Change`, and they are different releases by different bodies - the ADP report
is a private payroll estimate, rated Medium, that the author's session-long
rule is not about. Matching loosely would shut the whole session down for it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import httpx

from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.calendar import EventCalendar, MarketEvent

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
SOURCE_KEY = "FOREXFACTORY"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# How long one fetch is allowed to speak for. The feed's contents do not
# change this fast; our knowledge of them does, and a refresher that has been
# broken for half a day must not go on answering. Past this the calendar is
# STALE, which blocks - the same answer as no calendar at all, which is the
# right one when nobody can say what is scheduled.
MAX_VALIDITY = timedelta(hours=12)

# ForexFactory publishes by the US Eastern week, Sunday through Saturday, and
# only the current one. A fetch made on Saturday evening therefore knows
# nothing about Sunday, and an empty tail would look exactly like a quiet
# session. So validity never reaches past the week the fetch covers.
_FEED_ZONE = ZoneInfo("America/New_York")

_IMPACT_STARS: Mapping[str, int] = {"Low": 1, "Medium": 2, "High": 3}

# The BLS release the author's session-long rule is about, exactly.
_NFP_TITLE = "Non-Farm Employment Change"
_NFP_COUNTRY = "USD"


class EconomicCalendarError(RuntimeError):
    """Raised when the calendar could not be fetched or understood."""


class ForexFactoryCalendars:
    """Fetch the week's releases, and hold one fetch until it expires.

    Held rather than re-fetched per pass because the loop evaluates every five
    minutes and the feed changes daily. What is not held is an expired
    calendar: once `valid_until` passes, the next call fetches, and if that
    fetch fails the caller gets None and the strategy blocks.
    """

    def __init__(
        self,
        *,
        session_close_for: Callable[[datetime], datetime],
        client: httpx.AsyncClient | None = None,
        url: str = FEED_URL,
        max_validity: timedelta = MAX_VALIDITY,
    ) -> None:
        if type(url) is not str or not url.startswith("https://"):
            # The calendar decides whether the system is allowed to open a
            # position. Over cleartext, anybody on the path can empty it.
            raise ValueError("the calendar feed must be HTTPS")
        if max_validity <= timedelta(0):
            raise ValueError("max_validity must be positive")
        self._session_close_for = session_close_for
        self._client = client
        self._owned = client is None
        self._url = url
        self._max_validity = max_validity
        self._held: EventCalendar | None = None
        self.fetches = 0
        self.failures = 0

    async def aclose(self) -> None:
        if self._owned and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def calendar(self, now: datetime) -> EventCalendar | None:
        """The calendar in force, or None when there is none to give."""
        moment = require_utc(now)
        held = self._held
        if held is not None and held.captured_at <= moment < held.valid_until:
            return held
        try:
            fetched = await self._fetch(moment)
        except EconomicCalendarError:
            # Refusing to answer is the honest result. A stale calendar kept
            # past its validity would let the session run on a schedule
            # nobody has checked since this morning.
            self.failures += 1
            self._held = None
            return None
        self._held = fetched
        return fetched

    async def _fetch(self, now: datetime) -> EventCalendar:
        rows = await self._rows()
        self.fetches += 1
        valid_until = min(now + self._max_validity, _week_end(now))
        return EventCalendar(
            captured_at=now,
            valid_until=valid_until,
            events=_events(rows, now=now, session_close_for=self._session_close_for),
        )

    async def _rows(self) -> tuple[Mapping[str, object], ...]:
        client = self._client
        if client is None:
            client = httpx.AsyncClient()
            self._client = client
            self._owned = True
        try:
            response = await client.get(self._url, timeout=_TIMEOUT)
        except httpx.HTTPError as error:
            raise EconomicCalendarError("the calendar feed is unreachable") from error
        if response.status_code != 200:
            raise EconomicCalendarError(
                f"the calendar feed answered {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise EconomicCalendarError("the calendar feed is not JSON") from error
        if not isinstance(payload, list):
            raise EconomicCalendarError("the calendar feed is not a list")
        return tuple(
            cast("Mapping[str, object]", row)
            for row in cast("list[object]", payload)
            if isinstance(row, dict)
        )


def _week_end(now: datetime) -> datetime:
    """Midnight after the Saturday of the Eastern week `now` falls in."""
    eastern = now.astimezone(_FEED_ZONE)
    # Sunday starts the feed's week; Python's Monday is 0, so Sunday is 6.
    since_sunday = (eastern.weekday() + 1) % 7
    sunday = eastern.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=since_sunday
    )
    return (sunday + timedelta(days=7)).astimezone(UTC)


def _instant(value: object) -> datetime:
    if type(value) is not str:
        raise EconomicCalendarError("a calendar row carries no date")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise EconomicCalendarError(f"unreadable calendar date {value!r}") from error
    if parsed.tzinfo is None:
        # The feed states an offset on every row. One without is a row we
        # cannot place on the clock, and guessing UTC would move a release by
        # up to a day.
        raise EconomicCalendarError(f"calendar date {value!r} carries no offset")
    return parsed.astimezone(UTC)


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise EconomicCalendarError(f"a calendar row carries no {name}")
    return value.strip()


def _events(
    rows: Sequence[Mapping[str, object]],
    *,
    now: datetime,
    session_close_for: Callable[[datetime], datetime],
) -> tuple[MarketEvent, ...]:
    """The feed's rows as events, dropping what cannot block.

    Anything already past is dropped because the model forbids a calendar
    carrying an event from before its capture, and rightly: a calendar is
    evidence about what is scheduled, not about what happened. A blackout
    already running belongs to the fetch that saw the release coming, and that
    fetch stays in force until it expires.
    """
    return tuple(
        event
        for event in (
            _event(row, now=now, session_close_for=session_close_for) for row in rows
        )
        if event is not None
    )


def _event(
    row: Mapping[str, object],
    *,
    now: datetime,
    session_close_for: Callable[[datetime], datetime],
) -> MarketEvent | None:
    impact = row.get("impact")
    if not isinstance(impact, str):
        raise EconomicCalendarError("a calendar row carries no impact")
    stars = _IMPACT_STARS.get(impact)
    if stars is None:
        # `Holiday` and anything the feed adds later. Not a release, and not
        # something to invent a rating for.
        return None
    title = _text(row.get("title"), "title")
    country = _text(row.get("country"), "country")
    scheduled_at = _instant(row.get("date"))
    if scheduled_at < now:
        return None
    is_nfp = title == _NFP_TITLE and country == _NFP_COUNTRY
    return MarketEvent(
        event_id=f"{country}|{title}|{scheduled_at.isoformat()}",
        source_key=SOURCE_KEY,
        scheduled_at=scheduled_at,
        impact_stars=stars,
        # The feed carries no actual, so no release can be called
        # unsurprising. None is the two-hour window, not the ten-minute one.
        strong_surprise=None,
        is_nfp=is_nfp,
        session_close_at=session_close_for(scheduled_at) if is_nfp else None,
    )


def event_calendar_from(
    rows: Sequence[Mapping[str, object]],
    *,
    now: datetime,
    session_close_for: Callable[[datetime], datetime],
    max_validity: timedelta = MAX_VALIDITY,
) -> EventCalendar:
    """Decode rows without fetching them, for tests and for replay."""
    moment = require_utc(now)
    return EventCalendar(
        captured_at=moment,
        valid_until=min(moment + max_validity, _week_end(moment)),
        events=_events(rows, now=moment, session_close_for=session_close_for),
    )


__all__ = (
    "FEED_URL",
    "MAX_VALIDITY",
    "SOURCE_KEY",
    "EconomicCalendarError",
    "ForexFactoryCalendars",
    "event_calendar_from",
)
