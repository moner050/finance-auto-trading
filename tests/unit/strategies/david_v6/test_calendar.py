from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from autotrader.strategies.david_v6.calendar import (
    EventCalendar,
    MarketEvent,
    evaluate_event_window,
)
from autotrader.strategies.david_v6.models import EvidenceState

EVENT_AT = datetime(2026, 8, 24, 14, 30, tzinfo=UTC)


def _event(**changes: object) -> MarketEvent:
    values: dict[str, object] = {
        "event_id": "usd-cpi-2026-08",
        "source_key": "calendar-snapshot-2026-08-24",
        "scheduled_at": EVENT_AT,
        "impact_stars": 3,
        "strong_surprise": False,
        "is_nfp": False,
        "session_close_at": None,
    }
    values.update(changes)
    return MarketEvent(**values)  # type: ignore[arg-type]


def _calendar(*events: MarketEvent, **changes: object) -> EventCalendar:
    values: dict[str, object] = {
        "captured_at": EVENT_AT - timedelta(hours=6),
        "valid_until": EVENT_AT + timedelta(hours=8),
        "events": events,
    }
    values.update(changes)
    return EventCalendar(**values)  # type: ignore[arg-type]


def test_high_impact_blackout_uses_absolute_instants_and_half_open_end() -> None:
    event = _event()
    new_york = ZoneInfo("America/New_York")

    at_pre_boundary = evaluate_event_window(
        _calendar(event), (EVENT_AT - timedelta(minutes=10)).astimezone(new_york)
    )
    at_post_boundary = evaluate_event_window(
        _calendar(event), EVENT_AT + timedelta(minutes=10)
    )

    assert at_pre_boundary.state is EvidenceState.AVAILABLE
    assert at_pre_boundary.block_new_exposure is True
    assert at_pre_boundary.active_event_ids == (event.event_id,)
    assert at_post_boundary.block_new_exposure is False
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_event_window(_calendar(event), datetime(2026, 8, 24, 14, 20))


def test_unknown_surprise_and_nfp_apply_source_pinned_post_event_windows() -> None:
    unknown = _event(strong_surprise=None)
    nfp = _event(
        event_id="usd-nfp-2026-08",
        is_nfp=True,
        session_close_at=EVENT_AT + timedelta(hours=7),
    )

    unknown_facts = evaluate_event_window(
        _calendar(unknown), EVENT_AT + timedelta(minutes=119)
    )
    nfp_facts = evaluate_event_window(_calendar(nfp), EVENT_AT + timedelta(hours=6))

    assert unknown_facts.block_new_exposure is True
    assert unknown_facts.blackout_ends_at == EVENT_AT + timedelta(minutes=120)
    assert nfp_facts.block_new_exposure is True
    assert nfp_facts.blackout_ends_at == nfp.session_close_at


def test_a_stale_calendar_blocks_instead_of_assuming_nothing_changed() -> None:
    stale = _calendar(_event(), valid_until=EVENT_AT - timedelta(seconds=1))

    facts = evaluate_event_window(stale, EVENT_AT)

    assert facts.state is EvidenceState.STALE
    assert facts.block_new_exposure is True


def test_a_calendar_from_the_future_is_not_evidence_yet() -> None:
    ahead = _calendar(captured_at=EVENT_AT + timedelta(hours=1))

    facts = evaluate_event_window(ahead, EVENT_AT)

    assert facts.state is EvidenceState.UNKNOWN
    assert facts.block_new_exposure is True


def test_a_fetched_calendar_with_nothing_scheduled_permits_trading() -> None:
    """A quiet day is a fact, not an absence of one.

    The window belongs to the fetch, so an empty result still proves the
    calendar was read. Reading it off the events made a quiet day look
    exactly like a failed fetch and blocked trading forever.
    """
    quiet = _calendar()

    facts = evaluate_event_window(quiet, EVENT_AT)

    assert facts.state is EvidenceState.AVAILABLE
    assert facts.block_new_exposure is False
    assert facts.active_event_ids == ()


def test_a_calendar_must_be_the_exact_type() -> None:
    with pytest.raises(TypeError, match="exact EventCalendar"):
        evaluate_event_window((), EVENT_AT)  # type: ignore[arg-type]
