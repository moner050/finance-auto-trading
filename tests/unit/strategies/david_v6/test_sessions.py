from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from autotrader.strategies.david_v6.models import EvidenceState
from autotrader.strategies.david_v6.sessions import (
    ExchangeCalendar,
    SessionKind,
    evaluate_session,
)

OPEN = datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
EARLY_CLOSE = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def _us_calendar(**changes: object) -> ExchangeCalendar:
    values: dict[str, object] = {
        "session_date": date(2026, 11, 27),
        "kind": SessionKind.US_HLIT,
        "source_timezone": "America/New_York",
        "is_trading_day": True,
        "session_open_at": OPEN,
        "session_close_at": EARLY_CLOSE,
        "close_auction_at": None,
        "captured_at": OPEN - timedelta(days=1),
        "valid_until": EARLY_CLOSE,
    }
    values.update(changes)
    return ExchangeCalendar(**values)  # type: ignore[arg-type]


def test_early_close_drives_entry_and_flat_cutoffs() -> None:
    calendar = _us_calendar()

    before_cutoff = evaluate_session(
        calendar, EARLY_CLOSE - timedelta(minutes=30, seconds=1)
    )
    at_cutoff = evaluate_session(calendar, EARLY_CLOSE - timedelta(minutes=30))
    at_flat = evaluate_session(calendar, EARLY_CLOSE - timedelta(minutes=10))

    assert before_cutoff.entry_allowed is True
    assert at_cutoff.entry_allowed is False
    assert at_cutoff.reduce_only is True
    assert at_flat.must_be_flat is True
    assert at_flat.entry_cutoff_at == EARLY_CLOSE - timedelta(minutes=30)


def test_exchange_holiday_and_stale_calendar_fail_closed() -> None:
    holiday = _us_calendar(
        is_trading_day=False,
        session_open_at=None,
        session_close_at=None,
        valid_until=OPEN + timedelta(days=1),
    )
    stale = _us_calendar(valid_until=OPEN - timedelta(seconds=1))

    holiday_facts = evaluate_session(holiday, OPEN)
    stale_facts = evaluate_session(stale, OPEN)

    assert holiday_facts.state is EvidenceState.AVAILABLE
    assert holiday_facts.entry_allowed is False
    assert holiday_facts.blockers == ("EXCHANGE_HOLIDAY",)
    assert stale_facts.state is EvidenceState.STALE
    assert stale_facts.entry_allowed is False


def test_krx_uses_close_auction_boundary_and_binance_uses_utc_day() -> None:
    krx_open = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
    auction = datetime(2026, 8, 24, 6, 20, tzinfo=UTC)
    krx = ExchangeCalendar(
        session_date=date(2026, 8, 24),
        kind=SessionKind.KRX_HLIT,
        source_timezone="Asia/Seoul",
        is_trading_day=True,
        session_open_at=krx_open,
        session_close_at=datetime(2026, 8, 24, 6, 30, tzinfo=UTC),
        close_auction_at=auction,
        captured_at=krx_open - timedelta(days=1),
        valid_until=datetime(2026, 8, 24, 7, 0, tzinfo=UTC),
    )
    binance = ExchangeCalendar(
        session_date=date(2026, 8, 24),
        kind=SessionKind.BINANCE_USDM,
        source_timezone="UTC",
        is_trading_day=True,
        session_open_at=datetime(2026, 8, 24, tzinfo=UTC),
        session_close_at=datetime(2026, 8, 25, tzinfo=UTC),
        close_auction_at=None,
        captured_at=datetime(2026, 8, 23, tzinfo=UTC),
        valid_until=datetime(2026, 8, 25, tzinfo=UTC),
    )

    krx_facts = evaluate_session(krx, auction)
    binance_facts = evaluate_session(binance, datetime(2026, 8, 24, 23, 50, tzinfo=UTC))

    assert krx_facts.must_be_flat is True
    assert krx_facts.entry_cutoff_at == auction - timedelta(minutes=30)
    assert binance_facts.must_be_flat is True
    assert binance_facts.overnight_allowed is False
