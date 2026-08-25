from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.models import EvidenceState

_ENTRY_CUTOFF = timedelta(minutes=30)
_FLAT_CUTOFF = timedelta(minutes=10)
# Section 7.1 keeps the open tradeable and controls it by size, not by a
# waiting period: the v4 five minute anti-spike delay was withdrawn.
_OPEN_WINDOW = timedelta(minutes=15)
_OPEN_WINDOW_MULTIPLIER = Decimal("0.5")
_FULL_MULTIPLIER = Decimal(1)
_PRE_OPEN_MAX_MICRO_CONTRACTS = 3


class SessionKind(StrEnum):
    BINANCE_USDM = "BINANCE_USDM"
    KRX_HLIT = "KRX_HLIT"
    US_HLIT = "US_HLIT"
    CASH_METODO = "CASH_METODO"


_INTRADAY_OPEN_KINDS = frozenset({SessionKind.KRX_HLIT, SessionKind.US_HLIT})


@dataclass(frozen=True, slots=True)
class ExchangeCalendar:
    session_date: date
    kind: SessionKind
    source_timezone: str
    is_trading_day: bool
    session_open_at: datetime | None
    session_close_at: datetime | None
    close_auction_at: datetime | None
    pre_open_at: datetime | None
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
        for name in (
            "session_open_at",
            "session_close_at",
            "close_auction_at",
            "pre_open_at",
        ):
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
                    self.pre_open_at,
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
        if self.pre_open_at is not None and self.pre_open_at >= self.session_open_at:
            raise ValueError("pre-open must start before the session open")


@dataclass(frozen=True, slots=True)
class SessionFacts:
    state: EvidenceState
    session_open: bool
    entry_allowed: bool
    reduce_only: bool
    must_be_flat: bool
    overnight_allowed: bool
    pre_open: bool
    size_multiplier: Decimal
    max_micro_contracts: int | None
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
            pre_open=False,
            size_multiplier=_FULL_MULTIPLIER,
            max_micro_contracts=None,
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
    pre_open = (
        calendar.pre_open_at is not None
        and calendar.kind in _INTRADAY_OPEN_KINDS
        and calendar.pre_open_at <= decision < calendar.session_open_at
    )
    entry_allowed = (session_open and decision < entry_cutoff) or pre_open
    must_be_flat = session_open and decision >= flat_at
    blockers: list[str] = []
    if not session_open and not pre_open:
        blockers.append("SESSION_CLOSED")
    if session_open and decision >= entry_cutoff:
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
        pre_open=pre_open,
        size_multiplier=_size_multiplier(calendar, decision, pre_open=pre_open),
        max_micro_contracts=_PRE_OPEN_MAX_MICRO_CONTRACTS if pre_open else None,
        entry_cutoff_at=entry_cutoff,
        flat_at=flat_at,
        blockers=tuple(sorted(blockers)),
    )


def _size_multiplier(
    calendar: ExchangeCalendar,
    decision: datetime,
    *,
    pre_open: bool,
) -> Decimal:
    """Halve size for the first fifteen minutes of an intraday open."""
    if pre_open or calendar.kind not in _INTRADAY_OPEN_KINDS:
        return _FULL_MULTIPLIER
    assert calendar.session_open_at is not None
    if calendar.session_open_at <= decision < calendar.session_open_at + _OPEN_WINDOW:
        return _OPEN_WINDOW_MULTIPLIER
    return _FULL_MULTIPLIER


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
        pre_open=False,
        size_multiplier=_FULL_MULTIPLIER,
        max_micro_contracts=None,
        entry_cutoff_at=None,
        flat_at=None,
        blockers=(blocker,),
    )


def _require_datetime(value: object, name: str) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{name} must be an exact datetime")
    return require_utc(value)


__all__ = ("ExchangeCalendar", "SessionFacts", "SessionKind", "evaluate_session")
