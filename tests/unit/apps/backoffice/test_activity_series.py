"""The day of evaluation the dashboard draws.

The chart is the first thing on the operations screen, and the number an
operator acts on is not the tallest bar - it is the gaps. An hour with no
evaluation means the loop was not running, and that has to survive the trip
from the query to the page rather than being an hour the query simply did not
return.

These cover the reading, not the SQL: what the view says about a series is
what the template prints.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from autotrader.apps.backoffice.read_model import ActivityBucket, OperationsView

HOUR = datetime(2026, 9, 2, 0, tzinfo=UTC)


def _series(
    *counts: tuple[int, int, int | None, int | None],
) -> tuple[ActivityBucket, ...]:
    return tuple(
        ActivityBucket(
            hour=HOUR + timedelta(hours=index),
            decisions=decisions,
            accepted=accepted,
            best_matched=matched,
            fewest_blockers=blockers,
        )
        for index, (decisions, accepted, matched, blockers) in enumerate(counts)
    )


def _view(buckets: tuple[ActivityBucket, ...]) -> OperationsView:
    return OperationsView(
        controls=(),
        decisions=(),
        activity=buckets,
        positions=(),
        drifts=(),
        incidents=(),
    )


def test_an_hour_with_no_evaluation_is_idle() -> None:
    empty, busy = _series((0, 0, None, None), (5, 0, 2, 6))

    assert empty.idle
    assert not busy.idle


def test_the_scale_is_the_tallest_hour() -> None:
    view = _view(_series((3, 0, 1, 8), (11, 0, 3, 5), (7, 0, 2, 6)))

    assert view.busiest_hour == 11


def test_an_empty_day_still_scales() -> None:
    """The bar heights divide by this. A day the loop never ran must draw a
    flat axis rather than raise on the page."""
    view = _view(_series((0, 0, None, None), (0, 0, None, None)))

    assert view.busiest_hour == 1
    assert view.idle_hours == 2
    assert view.evaluated_recently == 0


def test_the_closest_hour_is_the_one_that_matched_most() -> None:
    view = _view(
        _series(
            (9, 0, 2, 5),
            (2, 0, 4, 6),
            (30, 0, 1, 4),
        )
    )

    closest = view.closest_recently
    assert closest is not None
    # Not the busiest hour and not the one with fewest blockers: an hour
    # blocked by one thing having matched almost nothing is not close.
    assert closest.best_matched == 4


def test_fewest_blockers_breaks_a_tie_on_matches() -> None:
    view = _view(_series((4, 0, 3, 7), (4, 0, 3, 5)))

    closest = view.closest_recently
    assert closest is not None
    assert closest.fewest_blockers == 5


def test_a_day_with_nothing_measured_has_no_closest_hour() -> None:
    view = _view(_series((0, 0, None, None), (0, 0, None, None)))

    assert view.closest_recently is None


def test_accepted_setups_are_counted_across_the_day() -> None:
    view = _view(_series((6, 0, 3, 5), (4, 1, 5, 0), (2, 0, 2, 6)))

    assert view.accepted_recently == 1
    assert view.evaluated_recently == 12
