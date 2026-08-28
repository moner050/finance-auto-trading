"""What a Shadow or Paper session is, and when a run of them counts.

Section 17 is the constraint that shapes this: the back office is an interface
to existing authority, not a source of one, and it cannot convert missing
evidence into readiness or edit a promotion state. So the authority lives here,
in a module that has no database and no screen, and both of those read it.

A session is one exchange date. Not a timestamp and not a window: a trading day
is the unit an operator watched and the unit evidence accumulates under, and
two claims on one date are that day observed twice rather than two days.

A session is pinned to the strategy manifest it ran under. If the source or the
configuration hash moves, earlier sessions were sessions of something else, and
counting them toward the new thing is how a rewritten strategy inherits a
reputation it never earned.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from autotrader.shared.time import require_utc

# Section 11.7: two distinct Shadow sessions and two distinct Paper sessions.
# Distinct means distinct exchange dates.
REQUIRED_SESSIONS = 2


class PromotionMode(StrEnum):
    SHADOW = "SHADOW"
    PAPER = "PAPER"


class SessionStatus(StrEnum):
    CLAIMED = "CLAIMED"
    COMPLETE = "COMPLETE"


class PromotionBlocker(StrEnum):
    """Why a manifest is not verified.

    Named rather than free text so a screen, a log and a test all say the same
    thing about the same situation.
    """

    NO_DECISIONS = "NO_DECISIONS"
    BLOCKING_INCIDENT = "BLOCKING_INCIDENT"
    BLOCKING_RECONCILIATION = "BLOCKING_RECONCILIATION"
    UNRESOLVED_UNKNOWN_ORDER = "UNRESOLVED_UNKNOWN_ORDER"
    NO_ORDERS = "NO_ORDERS"
    SESSION_NOT_OVER = "SESSION_NOT_OVER"


@dataclass(frozen=True, slots=True)
class SessionEvidence:
    """What the day left behind, counted rather than described.

    Counts, not booleans: "three blocking incidents" and "one" are different
    days, and a screen that flattened them would hide the difference at the
    moment it mattered.
    """

    decision_count: int
    order_count: int
    blocking_incident_count: int
    blocking_reconciliation_count: int
    unresolved_unknown_count: int

    def __post_init__(self) -> None:
        for name in (
            "decision_count",
            "order_count",
            "blocking_incident_count",
            "blocking_reconciliation_count",
            "unresolved_unknown_count",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class PromotionSession:
    """One claimed exchange date for one binding."""

    id: UUID
    binding_id: UUID
    account_id: UUID
    manifest_id: UUID
    mode: PromotionMode
    exchange_date: date
    status: SessionStatus
    claimed_at: datetime
    completed_at: datetime | None

    def __post_init__(self) -> None:
        if type(self.mode) is not PromotionMode:
            raise TypeError("mode must be an exact PromotionMode")
        if type(self.status) is not SessionStatus:
            raise TypeError("status must be an exact SessionStatus")
        if type(self.exchange_date) is not date:
            raise TypeError("exchange_date must be an exact date")
        object.__setattr__(self, "claimed_at", require_utc(self.claimed_at))
        if self.status is SessionStatus.COMPLETE:
            if self.completed_at is None:
                raise ValueError("a complete session has a completion time")
            moment = require_utc(self.completed_at)
            if moment < self.claimed_at:
                raise ValueError("a session cannot complete before it was claimed")
            object.__setattr__(self, "completed_at", moment)
        elif self.completed_at is not None:
            raise ValueError("an incomplete session has no completion time")


def verify(
    evidence: SessionEvidence,
    *,
    mode: PromotionMode,
    exchange_date: date,
    today: date,
) -> tuple[PromotionBlocker, ...]:
    """Why this session cannot be completed, or nothing.

    Section 11.7 says complete only a fully verified manifest, so this returns
    the reasons rather than a verdict: an operator who cannot see why is being
    asked to trust a refusal.
    """
    if type(mode) is not PromotionMode:
        raise TypeError("mode must be an exact PromotionMode")
    blockers: list[PromotionBlocker] = []
    if exchange_date >= today:
        # A day still running has evidence still arriving. Completing it early
        # would freeze a manifest that the rest of the day could contradict.
        blockers.append(PromotionBlocker.SESSION_NOT_OVER)
    if evidence.decision_count == 0:
        # A day the strategy never evaluated is a day nothing was learned on.
        blockers.append(PromotionBlocker.NO_DECISIONS)
    if mode is PromotionMode.PAPER and evidence.order_count == 0:
        # Shadow evaluates without ordering; Paper that never ordered did not
        # exercise the path Paper exists to exercise.
        blockers.append(PromotionBlocker.NO_ORDERS)
    if evidence.blocking_incident_count:
        blockers.append(PromotionBlocker.BLOCKING_INCIDENT)
    if evidence.blocking_reconciliation_count:
        blockers.append(PromotionBlocker.BLOCKING_RECONCILIATION)
    if evidence.unresolved_unknown_count:
        # An order whose outcome is unknown might have traded. A day holding
        # one has not been accounted for.
        blockers.append(PromotionBlocker.UNRESOLVED_UNKNOWN_ORDER)
    return tuple(blockers)


@dataclass(frozen=True, slots=True)
class ModeProgress:
    """How far one mode has got, and on which dates."""

    mode: PromotionMode
    completed_dates: tuple[date, ...]

    @property
    def satisfied(self) -> bool:
        return len(self.completed_dates) >= REQUIRED_SESSIONS

    @property
    def remaining(self) -> int:
        return max(0, REQUIRED_SESSIONS - len(self.completed_dates))


@dataclass(frozen=True, slots=True)
class PromotionState:
    """Where a binding stands against the requirement, under one manifest."""

    manifest_id: UUID
    shadow: ModeProgress
    paper: ModeProgress

    @property
    def ready(self) -> bool:
        """Both requirements met. This is what LIVE activation asks."""
        return self.shadow.satisfied and self.paper.satisfied


def promotion_state(
    sessions: tuple[PromotionSession, ...], *, manifest_id: UUID
) -> PromotionState:
    """Progress under one manifest, counting distinct dates.

    Sessions under other manifests are ignored rather than refused: they are
    real sessions of a different strategy build, and the caller asked about
    this one.
    """
    completed: dict[PromotionMode, set[date]] = {
        PromotionMode.SHADOW: set(),
        PromotionMode.PAPER: set(),
    }
    for session in sessions:
        if session.manifest_id != manifest_id:
            continue
        if session.status is not SessionStatus.COMPLETE:
            continue
        completed[session.mode].add(session.exchange_date)
    return PromotionState(
        manifest_id=manifest_id,
        shadow=ModeProgress(
            mode=PromotionMode.SHADOW,
            completed_dates=tuple(sorted(completed[PromotionMode.SHADOW])),
        ),
        paper=ModeProgress(
            mode=PromotionMode.PAPER,
            completed_dates=tuple(sorted(completed[PromotionMode.PAPER])),
        ),
    )


__all__ = (
    "REQUIRED_SESSIONS",
    "ModeProgress",
    "PromotionBlocker",
    "PromotionMode",
    "PromotionSession",
    "PromotionState",
    "SessionEvidence",
    "SessionStatus",
    "promotion_state",
    "verify",
)
