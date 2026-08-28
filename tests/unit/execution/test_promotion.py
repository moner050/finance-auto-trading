"""When a run of Shadow and Paper sessions counts.

Section 17 forbids the back office from converting missing evidence into
readiness. That only means something if there is somewhere else the answer
comes from, and this is it — no database, no screen, just the rule.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from autotrader.execution.promotion.models import (
    REQUIRED_SESSIONS,
    PromotionBlocker,
    PromotionMode,
    PromotionSession,
    SessionEvidence,
    SessionStatus,
    promotion_state,
    verify,
)
from autotrader.shared.ids import new_uuid7

TODAY = date(2026, 8, 27)
YESTERDAY = date(2026, 8, 26)
CLAIMED = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
COMPLETED = datetime(2026, 8, 26, 17, 0, tzinfo=UTC)
MANIFEST = new_uuid7()
BINDING = new_uuid7()
ACCOUNT = new_uuid7()


def _evidence(**changes: int) -> SessionEvidence:
    values: dict[str, int] = {
        "decision_count": 12,
        "order_count": 3,
        "blocking_incident_count": 0,
        "blocking_reconciliation_count": 0,
        "unresolved_unknown_count": 0,
    }
    values.update(changes)
    return SessionEvidence(**values)


def _session(
    *,
    mode: PromotionMode = PromotionMode.SHADOW,
    exchange_date: date = YESTERDAY,
    status: SessionStatus = SessionStatus.COMPLETE,
    manifest_id: object = None,
) -> PromotionSession:
    return PromotionSession(
        id=new_uuid7(),
        binding_id=BINDING,
        account_id=ACCOUNT,
        manifest_id=MANIFEST if manifest_id is None else manifest_id,  # type: ignore[arg-type]
        mode=mode,
        exchange_date=exchange_date,
        status=status,
        claimed_at=CLAIMED,
        completed_at=COMPLETED if status is SessionStatus.COMPLETE else None,
    )


# --- verifying one session ------------------------------------------------


def test_a_clean_finished_day_has_no_blockers() -> None:
    assert (
        verify(
            _evidence(),
            mode=PromotionMode.PAPER,
            exchange_date=YESTERDAY,
            today=TODAY,
        )
        == ()
    )


def test_a_day_still_running_cannot_be_completed() -> None:
    """Evidence is still arriving; freezing the manifest now would freeze a
    day the rest of which could contradict it."""
    blockers = verify(
        _evidence(), mode=PromotionMode.SHADOW, exchange_date=TODAY, today=TODAY
    )

    assert PromotionBlocker.SESSION_NOT_OVER in blockers


def test_a_day_the_strategy_never_evaluated_counts_for_nothing() -> None:
    blockers = verify(
        _evidence(decision_count=0),
        mode=PromotionMode.SHADOW,
        exchange_date=YESTERDAY,
        today=TODAY,
    )

    assert PromotionBlocker.NO_DECISIONS in blockers


def test_paper_that_never_ordered_did_not_exercise_paper() -> None:
    blockers = verify(
        _evidence(order_count=0),
        mode=PromotionMode.PAPER,
        exchange_date=YESTERDAY,
        today=TODAY,
    )

    assert PromotionBlocker.NO_ORDERS in blockers


def test_shadow_is_not_expected_to_order() -> None:
    """Shadow evaluates without ordering, so no orders is the normal case."""
    blockers = verify(
        _evidence(order_count=0),
        mode=PromotionMode.SHADOW,
        exchange_date=YESTERDAY,
        today=TODAY,
    )

    assert PromotionBlocker.NO_ORDERS not in blockers


@pytest.mark.parametrize(
    ("field", "blocker"),
    (
        ("blocking_incident_count", PromotionBlocker.BLOCKING_INCIDENT),
        ("blocking_reconciliation_count", PromotionBlocker.BLOCKING_RECONCILIATION),
        ("unresolved_unknown_count", PromotionBlocker.UNRESOLVED_UNKNOWN_ORDER),
    ),
)
def test_an_unresolved_problem_blocks_the_manifest(
    field: str, blocker: PromotionBlocker
) -> None:
    blockers = verify(
        _evidence(**{field: 1}),
        mode=PromotionMode.PAPER,
        exchange_date=YESTERDAY,
        today=TODAY,
    )

    assert blocker in blockers


def test_every_reason_is_reported_not_just_the_first() -> None:
    """An operator who fixes one blocker and meets the next is being told the
    truth one piece at a time, which is worse than being told all of it."""
    blockers = verify(
        _evidence(decision_count=0, order_count=0, blocking_incident_count=2),
        mode=PromotionMode.PAPER,
        exchange_date=TODAY,
        today=TODAY,
    )

    assert set(blockers) == {
        PromotionBlocker.SESSION_NOT_OVER,
        PromotionBlocker.NO_DECISIONS,
        PromotionBlocker.NO_ORDERS,
        PromotionBlocker.BLOCKING_INCIDENT,
    }


def test_a_negative_count_is_refused() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        _evidence(decision_count=-1)


# --- the run of sessions --------------------------------------------------


def test_two_sessions_on_one_date_are_one_day_observed_twice() -> None:
    sessions = (
        _session(exchange_date=YESTERDAY),
        _session(exchange_date=YESTERDAY),
    )

    state = promotion_state(sessions, manifest_id=MANIFEST)

    assert state.shadow.completed_dates == (YESTERDAY,)
    assert state.shadow.satisfied is False


def test_two_distinct_dates_satisfy_a_mode() -> None:
    sessions = (
        _session(exchange_date=date(2026, 8, 25)),
        _session(exchange_date=YESTERDAY),
    )

    state = promotion_state(sessions, manifest_id=MANIFEST)

    assert state.shadow.satisfied is True
    assert state.shadow.remaining == 0


def test_a_claimed_session_is_not_a_completed_one() -> None:
    sessions = (_session(status=SessionStatus.CLAIMED),)

    state = promotion_state(sessions, manifest_id=MANIFEST)

    assert state.shadow.completed_dates == ()


def test_readiness_needs_both_modes() -> None:
    shadow_only = tuple(_session(exchange_date=date(2026, 8, day)) for day in (24, 25))

    state = promotion_state(shadow_only, manifest_id=MANIFEST)

    assert state.shadow.satisfied is True
    assert state.paper.satisfied is False
    assert state.ready is False


def test_both_modes_at_two_distinct_dates_is_ready() -> None:
    sessions = tuple(
        _session(mode=mode, exchange_date=date(2026, 8, day))
        for mode in (PromotionMode.SHADOW, PromotionMode.PAPER)
        for day in (24, 25)
    )

    state = promotion_state(sessions, manifest_id=MANIFEST)

    assert state.ready is True


def test_sessions_under_another_manifest_do_not_count() -> None:
    """A rewritten strategy would otherwise inherit a reputation it never
    earned."""
    other = new_uuid7()
    sessions = tuple(
        _session(mode=mode, exchange_date=date(2026, 8, day), manifest_id=other)
        for mode in (PromotionMode.SHADOW, PromotionMode.PAPER)
        for day in (24, 25)
    )

    state = promotion_state(sessions, manifest_id=MANIFEST)

    assert state.ready is False
    assert state.shadow.remaining == REQUIRED_SESSIONS


def test_a_session_cannot_complete_before_it_was_claimed() -> None:
    with pytest.raises(ValueError, match="before it was claimed"):
        PromotionSession(
            id=new_uuid7(),
            binding_id=BINDING,
            account_id=ACCOUNT,
            manifest_id=MANIFEST,
            mode=PromotionMode.SHADOW,
            exchange_date=YESTERDAY,
            status=SessionStatus.COMPLETE,
            claimed_at=COMPLETED,
            completed_at=CLAIMED,
        )


def test_an_incomplete_session_carries_no_completion_time() -> None:
    with pytest.raises(ValueError, match="no completion time"):
        PromotionSession(
            id=new_uuid7(),
            binding_id=BINDING,
            account_id=ACCOUNT,
            manifest_id=MANIFEST,
            mode=PromotionMode.SHADOW,
            exchange_date=YESTERDAY,
            status=SessionStatus.CLAIMED,
            claimed_at=CLAIMED,
            completed_at=COMPLETED,
        )
