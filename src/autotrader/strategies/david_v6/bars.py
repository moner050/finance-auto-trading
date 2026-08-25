from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.strategies.david_v6.models import EvidenceState


def exact_completed_series(
    bars: Sequence[CompletedOhlcvBar],
    *,
    timeframe: timedelta,
    decision_at: datetime,
    required: int,
) -> tuple[CompletedOhlcvBar, ...]:
    _require_positive_timeframe(timeframe)
    _require_utc(decision_at, "decision_at")
    if type(required) is not int or required <= 0:
        raise ValueError("required must be a positive integer")
    values = tuple(bars)
    if any(type(bar) is not CompletedOhlcvBar for bar in values):
        return ()
    timestamps = tuple(bar.timestamp for bar in values)
    if any(current <= previous for previous, current in pairwise(timestamps)):
        return ()
    completed = tuple(bar for bar in values if bar.timestamp + timeframe <= decision_at)
    if len(completed) < required:
        return ()
    selected = completed[-required:]
    if any(
        current.timestamp - previous.timestamp != timeframe
        for previous, current in pairwise(selected)
    ):
        return ()
    return selected


def evidence_state(
    *,
    observed_at: datetime | None,
    decision_at: datetime,
    maximum_age: timedelta,
    applicable: bool = True,
) -> EvidenceState:
    _require_utc(decision_at, "decision_at")
    if type(maximum_age) is not timedelta or maximum_age < timedelta(0):
        raise ValueError("maximum_age must be a non-negative timedelta")
    if type(applicable) is not bool:
        raise TypeError("applicable must be bool")
    if not applicable:
        return EvidenceState.NOT_APPLICABLE
    if observed_at is None:
        return EvidenceState.UNAVAILABLE
    _require_utc(observed_at, "observed_at")
    if observed_at > decision_at:
        return EvidenceState.UNKNOWN
    if decision_at - observed_at <= maximum_age:
        return EvidenceState.AVAILABLE
    return EvidenceState.STALE


def _require_positive_timeframe(value: object) -> None:
    if type(value) is not timedelta or value <= timedelta(0):
        raise ValueError("timeframe must be a positive timedelta")


def _require_utc(value: object, name: str) -> None:
    if type(value) is not datetime or value.tzinfo is not UTC:
        raise ValueError(f"{name} must use exact UTC")
