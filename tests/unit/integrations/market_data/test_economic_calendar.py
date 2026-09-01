"""Translating a public feed into the author's news filter.

Every case here is one where a wrong answer is invisible. A mis-mapped impact
either blocks a session that should have traded or trades through a release
the author's rules prohibit, and neither announces itself: the calendar looks
fetched, the decision looks reasoned, and only the star rating was wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from autotrader.integrations.market_data.economic_calendar import (
    MAX_VALIDITY,
    SOURCE_KEY,
    EconomicCalendarError,
    ForexFactoryCalendars,
    event_calendar_from,
)
from autotrader.strategies.david_v6.calendar import evaluate_event_window

# A Tuesday, so the week end is several days out and does not cap validity.
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _row(
    *,
    title: str = "ISM Manufacturing PMI",
    country: str = "USD",
    impact: str = "High",
    at: datetime | None = None,
) -> dict[str, object]:
    moment = at if at is not None else NOW + timedelta(hours=2)
    return {
        "title": title,
        "country": country,
        "date": moment.isoformat(),
        "impact": impact,
        "forecast": "",
        "previous": "",
    }


def _close_for(moment: datetime) -> datetime:
    """A session close a day after the release, as the venue helper gives."""
    return moment + timedelta(hours=8)


def _decode(*rows: dict[str, object], now: datetime = NOW):
    return event_calendar_from(rows, now=now, session_close_for=_close_for)


def test_the_feed_rating_maps_onto_the_star_scale() -> None:
    """Section 8 blocks two and three stars. The feed's Low/Medium/High is
    that scale under other names, so this is the whole of the translation."""
    calendar = _decode(
        _row(impact="Low", title="a"),
        _row(impact="Medium", title="b"),
        _row(impact="High", title="c"),
    )

    assert {event.impact_stars for event in calendar.events} == {1, 2, 3}


def test_a_holiday_is_not_given_a_rating_it_never_had() -> None:
    """A holiday is not a release. Assigning it a star would put a number the
    source never gave into evidence, and only two stars and above block, so
    dropping it costs nothing."""
    calendar = _decode(_row(impact="Holiday", title="Bank Holiday"))

    assert calendar.events == ()


def test_no_release_is_called_unsurprising() -> None:
    """The feed carries forecast and previous but no actual, so whether a
    release surprised is not knowable when the calendar is fetched. None
    routes to the two-hour window rather than the ten-minute one."""
    calendar = _decode(_row())

    assert calendar.events[0].strong_surprise is None


def test_the_employment_report_is_matched_exactly() -> None:
    """The feed carries `ADP Non-Farm Employment Change` too, and it is a
    private estimate rated Medium rather than the release section 8 blocks for
    a whole session. A substring match would shut the session down for it."""
    calendar = _decode(
        _row(title="Non-Farm Employment Change", country="USD"),
        _row(title="ADP Non-Farm Employment Change", country="USD", impact="Medium"),
        _row(title="Employment Change", country="CAD"),
    )

    by_title = {event.event_id.split("|")[1]: event for event in calendar.events}
    assert by_title["Non-Farm Employment Change"].is_nfp is True
    assert by_title["ADP Non-Farm Employment Change"].is_nfp is False
    assert by_title["Employment Change"].is_nfp is False


def test_the_employment_report_blocks_until_the_session_closes() -> None:
    release = NOW + timedelta(hours=2)
    calendar = _decode(_row(title="Non-Farm Employment Change", at=release))

    facts = evaluate_event_window(calendar, release + timedelta(hours=3))

    assert facts.block_new_exposure is True
    assert facts.blackout_ends_at == _close_for(release)


def test_events_sharing_an_instant_stay_distinct() -> None:
    """Three RBNZ releases land on the same timestamp and differ only by
    title. An identity built from the instant alone would collapse them and
    raise a payload collision."""
    at = NOW + timedelta(hours=3)
    calendar = _decode(
        _row(title="Official Cash Rate", country="NZD", at=at),
        _row(title="RBNZ Rate Statement", country="NZD", at=at),
        _row(title="RBNZ Press Conference", country="NZD", at=at),
    )

    assert len({event.event_id for event in calendar.events}) == 3


def test_an_already_past_release_is_not_carried() -> None:
    """The model forbids a calendar carrying an event from before its capture:
    a calendar is evidence about what is scheduled, not about what happened."""
    calendar = _decode(_row(at=NOW - timedelta(minutes=5)))

    assert calendar.events == ()


def test_validity_never_reaches_past_the_feed_s_week() -> None:
    """The feed publishes one Eastern week and `nextweek` answers 404. A
    calendar fetched on Saturday knows nothing about Sunday, and an empty tail
    would look exactly like a quiet session."""
    saturday = datetime(2026, 9, 5, 22, 0, tzinfo=UTC)

    calendar = _decode(now=saturday)

    assert calendar.valid_until < saturday + MAX_VALIDITY
    assert calendar.valid_until == datetime(2026, 9, 6, 4, 0, tzinfo=UTC)


def test_an_ordinary_week_is_bounded_by_the_validity_instead() -> None:
    assert _decode().valid_until == NOW + MAX_VALIDITY


def test_a_row_without_an_offset_is_refused() -> None:
    """Guessing UTC would move a release by up to a day, and a release in the
    wrong place blocks the wrong hours."""
    row = _row()
    row["date"] = "2026-09-01T14:00:00"

    with pytest.raises(EconomicCalendarError, match="offset"):
        _decode(row)


def test_the_source_is_recorded_on_every_event() -> None:
    assert _decode(_row()).events[0].source_key == SOURCE_KEY


def _source(handler: object, **kwargs: object) -> ForexFactoryCalendars:
    return ForexFactoryCalendars(
        session_close_for=_close_for,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)  # type: ignore[arg-type]
        ),
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_one_fetch_serves_until_it_expires() -> None:
    """The loop evaluates every five minutes and the feed changes daily.
    Re-fetching per pass would be a request a minute for an answer that did
    not change."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[_row()])

    source = _source(handler)
    await source.calendar(NOW)
    await source.calendar(NOW + timedelta(minutes=5))
    await source.calendar(NOW + timedelta(hours=1))

    assert calls == 1
    await source.aclose()


@pytest.mark.asyncio
async def test_an_expired_calendar_is_refetched() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[_row(at=NOW + timedelta(days=1))])

    source = _source(handler)
    await source.calendar(NOW)
    await source.calendar(NOW + MAX_VALIDITY + timedelta(minutes=1))

    assert calls == 2
    await source.aclose()


@pytest.mark.asyncio
async def test_a_failed_fetch_answers_with_nothing_rather_than_a_stale_one() -> None:
    """None makes the strategy block, which is the right answer when nobody
    can say what is scheduled. Serving the expired calendar instead would let
    a session run on a schedule that has not been checked since morning."""
    responses = [httpx.Response(200, json=[_row()]), httpx.Response(503)]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    source = _source(handler)
    assert await source.calendar(NOW) is not None

    expired = NOW + MAX_VALIDITY + timedelta(minutes=1)
    assert await source.calendar(expired) is None
    assert source.failures == 1
    await source.aclose()


@pytest.mark.asyncio
async def test_an_unreachable_feed_answers_with_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    source = _source(handler)

    assert await source.calendar(NOW) is None
    await source.aclose()


def test_a_cleartext_feed_is_refused() -> None:
    """The calendar decides whether a position may be opened at all. Over
    cleartext anybody on the path can empty it, and an empty calendar permits
    everything."""
    with pytest.raises(ValueError, match="HTTPS"):
        ForexFactoryCalendars(
            session_close_for=_close_for,
            url="http://nfs.faireconomy.media/ff_calendar_thisweek.json",
        )
