from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from autotrader.strategies.david_v6.calendar import MarketEvent, evaluate_event_window
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
        "calendar_captured_at": EVENT_AT - timedelta(hours=6),
        "calendar_valid_until": EVENT_AT + timedelta(hours=8),
    }
    values.update(changes)
    return MarketEvent(**values)  # type: ignore[arg-type]


def test_high_impact_blackout_uses_absolute_instants_and_half_open_end() -> None:
    event = _event()
    new_york = ZoneInfo("America/New_York")

    at_pre_boundary = evaluate_event_window(
        (event,), (EVENT_AT - timedelta(minutes=10)).astimezone(new_york)
    )
    at_post_boundary = evaluate_event_window((event,), EVENT_AT + timedelta(minutes=10))

    assert at_pre_boundary.state is EvidenceState.AVAILABLE
    assert at_pre_boundary.block_new_exposure is True
    assert at_pre_boundary.active_event_ids == (event.event_id,)
    assert at_post_boundary.block_new_exposure is False
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_event_window((event,), datetime(2026, 8, 24, 14, 20))


def test_unknown_surprise_and_nfp_apply_source_pinned_post_event_windows() -> None:
    unknown = _event(strong_surprise=None)
    nfp = _event(
        event_id="usd-nfp-2026-08",
        is_nfp=True,
        session_close_at=EVENT_AT + timedelta(hours=7),
    )

    unknown_facts = evaluate_event_window((unknown,), EVENT_AT + timedelta(minutes=119))
    nfp_facts = evaluate_event_window((nfp,), EVENT_AT + timedelta(hours=6))

    assert unknown_facts.block_new_exposure is True
    assert unknown_facts.blackout_ends_at == EVENT_AT + timedelta(minutes=120)
    assert nfp_facts.block_new_exposure is True
    assert nfp_facts.blackout_ends_at == nfp.session_close_at


def test_stale_or_empty_calendar_blocks_instead_of_assuming_no_events() -> None:
    stale = _event(calendar_valid_until=EVENT_AT - timedelta(seconds=1))

    stale_facts = evaluate_event_window((stale,), EVENT_AT)
    empty_facts = evaluate_event_window((), EVENT_AT)

    assert stale_facts.state is EvidenceState.STALE
    assert stale_facts.block_new_exposure is True
    assert empty_facts.state is EvidenceState.UNKNOWN
    assert empty_facts.block_new_exposure is True
