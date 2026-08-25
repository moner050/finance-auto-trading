from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.models import EvidenceState

_ENTRY_CUTOFF = timedelta(minutes=30)
_FLAT_CUTOFF = timedelta(minutes=10)


class SessionKind(StrEnum):
    BINANCE_USDM = "BINANCE_USDM"
    KRX_HLIT = "KRX_HLIT"
    US_HLIT = "US_HLIT"
    CASH_METODO = "CASH_METODO"


@dataclass(frozen=True, slots=True)
class ExchangeCalendar:
    session_date: date
    kind: SessionKind
    source_timezone: str
    is_trading_day: bool
    session_open_at: datetime | None
    session_close_at: datetime | None
    close_auction_at: datetime | None
    captured_at: datetime
    valid_until: datetime

    def __post_init__(self) -> None:
        if type(self.session_date) is not date:
            raise TypeError("session_date must be an exact date")
        if type(self.kind) is not SessionKind:
            raise TypeError("kind must be an exact SessionKind")
        if type(self.source_timezone) is not str or not self.source_timezone:
            raise ValueError("source_timezone must be non-empty text")
        try:
            source_zone = ZoneInfo(self.source_timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("source_timezone must be a valid IANA timezone") from error
        if type(self.is_trading_day) is not bool:
            raise TypeError("is_trading_day must be bool")
        for name in ("captured_at", "valid_until"):
            value = getattr(self, name)
            if type(value) is not datetime:
                raise TypeError(f"{name} must be an exact datetime")
            object.__setattr__(self, name, require_utc(value))
        for name in ("session_open_at", "session_close_at", "close_auction_at"):
            value = getattr(self, name)
            if value is None:
                continue
            if type(value) is not datetime:
                raise TypeError(f"{name} must be an exact datetime or None")
            object.__setattr__(self, name, require_utc(value))
        if self.valid_until < self.captured_at:
            raise ValueError("calendar validity cannot end before capture")
        if not self.is_trading_day:
            if any(
                value is not None
                for value in (
                    self.session_open_at,
                    self.session_close_at,
                    self.close_auction_at,
                )
            ):
                raise ValueError("holiday calendar cannot contain session boundaries")
            return
        if self.session_open_at is None or self.session_close_at is None:
            raise ValueError("trading day requires open and close boundaries")
        if self.session_close_at <= self.session_open_at:
            raise ValueError("session close must be later than open")
        if self.session_open_at.astimezone(source_zone).date() != self.session_date:
            raise ValueError("session_date must match the source-local open date")
        if self.kind is SessionKind.KRX_HLIT and (
            self.close_auction_at is None
            or not self.session_open_at < self.close_auction_at <= self.session_close_at
        ):
            raise ValueError("KRX HLIT requires an in-session close auction boundary")


@dataclass(frozen=True, slots=True)
class SessionFacts:
    state: EvidenceState
    session_open: bool
    entry_allowed: bool
    reduce_only: bool
    must_be_flat: bool
    overnight_allowed: bool
    entry_cutoff_at: datetime | None
    flat_at: datetime | None
    blockers: tuple[str, ...]


def evaluate_session(
    calendar: ExchangeCalendar,
    decision_at: datetime,
) -> SessionFacts:
    if type(calendar) is not ExchangeCalendar:
        raise TypeError("calendar must be an exact ExchangeCalendar")
    decision = _require_datetime(decision_at, "decision_at")
    overnight = calendar.kind is SessionKind.CASH_METODO
    if calendar.captured_at > decision:
        return _blocked(EvidenceState.UNKNOWN, overnight, "CALENDAR_FROM_FUTURE")
    if calendar.valid_until < decision:
        return _blocked(EvidenceState.STALE, overnight, "STALE_EXCHANGE_CALENDAR")
    if not calendar.is_trading_day:
        return _blocked(EvidenceState.AVAILABLE, overnight, "EXCHANGE_HOLIDAY")

    assert calendar.session_open_at is not None
    assert calendar.session_close_at is not None
    if calendar.kind is SessionKind.CASH_METODO:
        return SessionFacts(
            state=EvidenceState.AVAILABLE,
            session_open=calendar.session_open_at
            <= decision
            < calendar.session_close_at,
            entry_allowed=True,
            reduce_only=False,
            must_be_flat=False,
            overnight_allowed=True,
            entry_cutoff_at=None,
            flat_at=None,
            blockers=(),
        )

    close_reference = (
        calendar.close_auction_at
        if calendar.kind is SessionKind.KRX_HLIT
        else calendar.session_close_at
    )
    assert close_reference is not None
    entry_cutoff = close_reference - _ENTRY_CUTOFF
    flat_at = (
        close_reference
        if calendar.kind is SessionKind.KRX_HLIT
        else close_reference - _FLAT_CUTOFF
    )
    session_open = calendar.session_open_at <= decision < calendar.session_close_at
    entry_allowed = session_open and decision < entry_cutoff
    must_be_flat = decision >= flat_at
    blockers: list[str] = []
    if not session_open:
        blockers.append("SESSION_CLOSED")
    if decision >= entry_cutoff:
        blockers.append("ENTRY_CUTOFF_REACHED")
    if must_be_flat:
        blockers.append("FLAT_CUTOFF_REACHED")
    return SessionFacts(
        state=EvidenceState.AVAILABLE,
        session_open=session_open,
        entry_allowed=entry_allowed,
        reduce_only=not entry_allowed,
        must_be_flat=must_be_flat,
        overnight_allowed=False,
        entry_cutoff_at=entry_cutoff,
        flat_at=flat_at,
        blockers=tuple(sorted(blockers)),
    )


def _blocked(
    state: EvidenceState,
    overnight_allowed: bool,
    blocker: str,
) -> SessionFacts:
    return SessionFacts(
        state=state,
        session_open=False,
        entry_allowed=False,
        reduce_only=True,
        must_be_flat=False,
        overnight_allowed=overnight_allowed,
        entry_cutoff_at=None,
        flat_at=None,
        blockers=(blocker,),
    )


def _require_datetime(value: object, name: str) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{name} must be an exact datetime")
    return require_utc(value)


__all__ = ("ExchangeCalendar", "SessionFacts", "SessionKind", "evaluate_session")
