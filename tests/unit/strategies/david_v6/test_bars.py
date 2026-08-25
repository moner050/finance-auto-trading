from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.strategies.david_v6.bars import (
    evidence_state,
    exact_completed_series,
)
from autotrader.strategies.david_v6.models import EvidenceState

START = datetime(2026, 8, 24, tzinfo=UTC)
ONE_MINUTE = timedelta(minutes=1)


def _bar(minutes: int) -> CompletedOhlcvBar:
    price = Decimal(100 + minutes)
    return CompletedOhlcvBar(
        timestamp=START + minutes * ONE_MINUTE,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("1"),
    )


def test_exact_completed_series_excludes_the_forming_bar() -> None:
    bars = tuple(_bar(index) for index in range(4))

    result = exact_completed_series(
        bars,
        timeframe=ONE_MINUTE,
        decision_at=START + 3 * ONE_MINUTE,
        required=3,
    )

    assert tuple(bar.timestamp for bar in result) == (
        START,
        START + ONE_MINUTE,
        START + 2 * ONE_MINUTE,
    )


def test_exact_completed_series_does_not_replace_a_missing_middle_bar() -> None:
    bars = (_bar(0), _bar(1), _bar(3), _bar(4))

    result = exact_completed_series(
        bars,
        timeframe=ONE_MINUTE,
        decision_at=START + 5 * ONE_MINUTE,
        required=3,
    )

    assert result == ()


def test_exact_completed_series_rejects_duplicate_timestamps() -> None:
    bars = (_bar(0), _bar(1), _bar(1), _bar(2))

    result = exact_completed_series(
        bars,
        timeframe=ONE_MINUTE,
        decision_at=START + 3 * ONE_MINUTE,
        required=3,
    )

    assert result == ()


def test_evidence_state_uses_inclusive_age_boundary() -> None:
    decision_at = START + timedelta(seconds=30)

    assert (
        evidence_state(
            observed_at=START,
            decision_at=decision_at,
            maximum_age=timedelta(seconds=30),
        )
        is EvidenceState.AVAILABLE
    )
    assert (
        evidence_state(
            observed_at=START - timedelta(microseconds=1),
            decision_at=decision_at,
            maximum_age=timedelta(seconds=30),
        )
        is EvidenceState.STALE
    )


def test_evidence_state_distinguishes_absent_future_and_inapplicable() -> None:
    assert (
        evidence_state(
            observed_at=None,
            decision_at=START,
            maximum_age=timedelta(seconds=1),
        )
        is EvidenceState.UNAVAILABLE
    )
    assert (
        evidence_state(
            observed_at=START + timedelta(microseconds=1),
            decision_at=START,
            maximum_age=timedelta(seconds=1),
        )
        is EvidenceState.UNKNOWN
    )
    assert (
        evidence_state(
            observed_at=START + timedelta(days=1),
            decision_at=START,
            maximum_age=timedelta(seconds=1),
            applicable=False,
        )
        is EvidenceState.NOT_APPLICABLE
    )
