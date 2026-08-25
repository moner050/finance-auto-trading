from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.models import EvidenceState

_PRE_EVENT_BLOCK = timedelta(minutes=10)
_NORMAL_POST_EVENT_BLOCK = timedelta(minutes=10)
_STRONG_OR_UNKNOWN_POST_EVENT_BLOCK = timedelta(minutes=120)


@dataclass(frozen=True, slots=True)
class MarketEvent:
    event_id: str
    source_key: str
    scheduled_at: datetime
    impact_stars: int
    strong_surprise: bool | None
    is_nfp: bool
    session_close_at: datetime | None
    calendar_captured_at: datetime
    calendar_valid_until: datetime

    def __post_init__(self) -> None:
        _require_text(self.event_id, "event_id")
        _require_text(self.source_key, "source_key")
        if type(self.impact_stars) is not int or not 1 <= self.impact_stars <= 3:
            raise ValueError("impact_stars must be an integer from one through three")
        if self.strong_surprise is not None and type(self.strong_surprise) is not bool:
            raise TypeError("strong_surprise must be bool or None")
        if type(self.is_nfp) is not bool:
            raise TypeError("is_nfp must be bool")
        for name in (
            "scheduled_at",
            "calendar_captured_at",
            "calendar_valid_until",
        ):
            value = getattr(self, name)
            if type(value) is not datetime:
                raise TypeError(f"{name} must be an exact datetime")
            object.__setattr__(self, name, require_utc(value))
        if self.session_close_at is not None:
            if type(self.session_close_at) is not datetime:
                raise TypeError("session_close_at must be an exact datetime or None")
            object.__setattr__(
                self,
                "session_close_at",
                require_utc(self.session_close_at),
            )
        if self.calendar_valid_until < self.calendar_captured_at:
            raise ValueError("calendar validity cannot end before capture")
        if self.is_nfp and (
            self.session_close_at is None or self.session_close_at <= self.scheduled_at
        ):
            raise ValueError("NFP requires a later authoritative session close")


@dataclass(frozen=True, slots=True)
class CalendarFacts:
    state: EvidenceState
    block_new_exposure: bool
    close_intraday_positions: bool
    active_event_ids: tuple[str, ...]
    blackout_ends_at: datetime | None
    monday_score_penalty: int


def evaluate_event_window(
    events: Sequence[MarketEvent],
    decision_at: datetime,
) -> CalendarFacts:
    decision = _require_datetime(decision_at, "decision_at")
    if any(type(event) is not MarketEvent for event in events):
        raise TypeError("events must contain exact MarketEvent values")
    canonical = _deduplicate(tuple(events))
    penalty = 1 if decision.weekday() == 0 else 0
    if not canonical:
        return _blocked(EvidenceState.UNKNOWN, penalty)
    if any(event.calendar_captured_at > decision for event in canonical):
        return _blocked(EvidenceState.UNKNOWN, penalty)
    if any(event.calendar_valid_until < decision for event in canonical):
        return _blocked(EvidenceState.STALE, penalty)

    active: list[tuple[MarketEvent, datetime]] = []
    for event in canonical:
        if event.impact_stars < 2:
            continue
        blackout_end = _blackout_end(event)
        if event.scheduled_at - _PRE_EVENT_BLOCK <= decision < blackout_end:
            active.append((event, blackout_end))
    return CalendarFacts(
        state=EvidenceState.AVAILABLE,
        block_new_exposure=bool(active),
        close_intraday_positions=any(
            decision < event.scheduled_at for event, _ in active
        ),
        active_event_ids=tuple(sorted(event.event_id for event, _ in active)),
        blackout_ends_at=max((end for _, end in active), default=None),
        monday_score_penalty=penalty,
    )


def _blackout_end(event: MarketEvent) -> datetime:
    if event.is_nfp:
        assert event.session_close_at is not None
        return event.session_close_at
    if event.strong_surprise is False:
        return event.scheduled_at + _NORMAL_POST_EVENT_BLOCK
    return event.scheduled_at + _STRONG_OR_UNKNOWN_POST_EVENT_BLOCK


def _deduplicate(events: tuple[MarketEvent, ...]) -> tuple[MarketEvent, ...]:
    by_id: dict[str, MarketEvent] = {}
    for event in events:
        existing = by_id.get(event.event_id)
        if existing is not None and existing != event:
            raise ValueError("event identity payload collision")
        by_id[event.event_id] = event
    return tuple(
        sorted(by_id.values(), key=lambda event: (event.scheduled_at, event.event_id))
    )


def _blocked(state: EvidenceState, monday_score_penalty: int) -> CalendarFacts:
    return CalendarFacts(
        state=state,
        block_new_exposure=True,
        close_intraday_positions=False,
        active_event_ids=(),
        blackout_ends_at=None,
        monday_score_penalty=monday_score_penalty,
    )


def _require_datetime(value: object, name: str) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{name} must be an exact datetime")
    return require_utc(value)


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{name} must be non-empty trimmed text")
    return value


__all__ = ("CalendarFacts", "MarketEvent", "evaluate_event_window")
