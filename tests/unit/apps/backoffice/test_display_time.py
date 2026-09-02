"""The one place a stored moment becomes a written time.

The store is UTC and the screen is KST, and the conversion is a filter rather
than a habit each template follows - so this is where the rule is stated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from autotrader.apps.backoffice.display import KST, in_kst


def test_a_stored_moment_reads_nine_hours_later() -> None:
    assert in_kst(datetime(2026, 9, 2, 5, 58, 30, tzinfo=UTC)) == "09-02 14:58:30"


def test_the_date_rolls_with_the_clock() -> None:
    """Half past three in the afternoon Korean time on the second is half
    past six in the morning UTC - and an operator reading 09-01 for their own
    afternoon would place the whole night wrongly."""
    assert in_kst(datetime(2026, 9, 1, 16, 0, tzinfo=UTC), "%m-%d") == "09-02"


def test_a_naive_value_is_read_as_utc() -> None:
    """Everything here is UTC, including what a column hands back before the
    type attaches a zone. Guessing local would move a time nine hours with
    nothing on the screen saying so."""
    assert in_kst(datetime(2026, 9, 2, 5, 58, 30)) == "09-02 14:58:30"


def test_another_zone_is_converted_rather_than_relabelled() -> None:
    newyork = timezone(timedelta(hours=-4))
    assert in_kst(datetime(2026, 9, 2, 1, 58, 30, tzinfo=newyork)) == "09-02 14:58:30"


def test_nothing_is_a_dash_rather_than_a_blank() -> None:
    """An empty cell reads as a rendering fault. A dash reads as no value."""
    assert in_kst(None) == "-"


def test_only_a_datetime_can_be_shown_as_a_time() -> None:
    with pytest.raises(TypeError):
        in_kst("2026-09-02T05:58:30Z")  # type: ignore[arg-type]


def test_the_offset_is_korea_standard_time() -> None:
    assert KST.utcoffset(None) == timedelta(hours=9)
