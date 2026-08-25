from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from autotrader.strategies.david_v6.models import EvidenceState
from autotrader.strategies.david_v6.sessions import (
    ExchangeCalendar,
    KrxMarketSafety,
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
        "pre_open_at": None,
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
        pre_open_at=None,
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
        pre_open_at=None,
        captured_at=datetime(2026, 8, 23, tzinfo=UTC),
        valid_until=datetime(2026, 8, 25, tzinfo=UTC),
    )

    krx_facts = evaluate_session(krx, auction)
    binance_facts = evaluate_session(binance, datetime(2026, 8, 24, 23, 50, tzinfo=UTC))

    assert krx_facts.must_be_flat is True
    assert krx_facts.entry_cutoff_at == auction - timedelta(minutes=30)
    assert binance_facts.must_be_flat is True
    assert binance_facts.overnight_allowed is False


def test_first_fifteen_minutes_of_the_open_halve_the_size() -> None:
    """Section 7.1 controls the open by size, not by an anti-spike delay."""
    calendar = _us_calendar()

    at_open = evaluate_session(calendar, OPEN)
    late_in_window = evaluate_session(
        calendar, OPEN + timedelta(minutes=14, seconds=59)
    )
    after_window = evaluate_session(calendar, OPEN + timedelta(minutes=15))

    assert at_open.entry_allowed is True
    assert at_open.size_multiplier == Decimal("0.5")
    assert late_in_window.size_multiplier == Decimal("0.5")
    assert after_window.size_multiplier == Decimal(1)


def test_the_open_is_tradeable_from_the_first_second() -> None:
    facts = evaluate_session(_us_calendar(), OPEN + timedelta(seconds=22))

    assert facts.entry_allowed is True
    assert facts.blockers == ()


def test_pre_open_allows_a_capped_micro_entry() -> None:
    calendar = _us_calendar(pre_open_at=OPEN - timedelta(hours=5))

    facts = evaluate_session(calendar, OPEN - timedelta(minutes=2))

    assert facts.pre_open is True
    assert facts.entry_allowed is True
    assert facts.max_micro_contracts == 3
    assert facts.session_open is False
    assert facts.blockers == ()


def test_without_a_pre_open_boundary_there_is_no_pre_open_entry() -> None:
    facts = evaluate_session(_us_calendar(), OPEN - timedelta(minutes=2))

    assert facts.pre_open is False
    assert facts.entry_allowed is False
    assert facts.max_micro_contracts is None
    assert "SESSION_CLOSED" in facts.blockers


def test_pre_open_must_start_before_the_open() -> None:
    with pytest.raises(ValueError, match="pre-open must start before"):
        _us_calendar(pre_open_at=OPEN)


def test_in_session_entry_carries_no_micro_contract_cap() -> None:
    facts = evaluate_session(_us_calendar(), OPEN + timedelta(minutes=30))

    assert facts.max_micro_contracts is None
    assert facts.pre_open is False


def test_binance_is_continuous_and_never_halves_size() -> None:
    calendar = ExchangeCalendar(
        session_date=date(2026, 11, 27),
        kind=SessionKind.BINANCE_USDM,
        source_timezone="UTC",
        is_trading_day=True,
        session_open_at=datetime(2026, 11, 27, tzinfo=UTC),
        session_close_at=datetime(2026, 11, 28, tzinfo=UTC),
        close_auction_at=None,
        pre_open_at=None,
        captured_at=datetime(2026, 11, 26, tzinfo=UTC),
        valid_until=datetime(2026, 11, 28, tzinfo=UTC),
    )

    facts = evaluate_session(calendar, datetime(2026, 11, 27, 0, 5, tzinfo=UTC))

    assert facts.size_multiplier == Decimal(1)


def _krx_calendar(**changes: object) -> ExchangeCalendar:
    open_at = datetime(2026, 11, 27, 0, 0, tzinfo=UTC)
    values: dict[str, object] = {
        "session_date": date(2026, 11, 27),
        "kind": SessionKind.KRX_HLIT,
        "source_timezone": "Asia/Seoul",
        "is_trading_day": True,
        "session_open_at": open_at,
        "session_close_at": open_at + timedelta(hours=6, minutes=30),
        "close_auction_at": open_at + timedelta(hours=6, minutes=20),
        "pre_open_at": None,
        "captured_at": open_at - timedelta(days=1),
        "valid_until": open_at + timedelta(hours=7),
    }
    values.update(changes)
    return ExchangeCalendar(**values)  # type: ignore[arg-type]


def _safety(**changes: object) -> KrxMarketSafety:
    values: dict[str, object] = {
        "observed_at": datetime(2026, 11, 27, 1, 0, tzinfo=UTC),
        "has_active_krx_vi": False,
        "is_single_price_auction": False,
    }
    values.update(changes)
    return KrxMarketSafety(**values)  # type: ignore[arg-type]


KRX_MIDSESSION = datetime(2026, 11, 27, 1, 0, tzinfo=UTC)


def test_krx_without_market_safety_evidence_fails_closed() -> None:
    facts = evaluate_session(_krx_calendar(), KRX_MIDSESSION)

    assert facts.entry_allowed is False
    assert "KRX_MARKET_SAFETY_UNAVAILABLE" in facts.blockers


def test_krx_volatility_interruption_blocks_entry() -> None:
    facts = evaluate_session(
        _krx_calendar(),
        KRX_MIDSESSION,
        market_safety=_safety(has_active_krx_vi=True),
    )

    assert facts.entry_allowed is False
    assert facts.reduce_only is True
    assert "KRX_VI_ACTIVE" in facts.blockers


def test_krx_single_price_auction_blocks_entry() -> None:
    facts = evaluate_session(
        _krx_calendar(),
        KRX_MIDSESSION,
        market_safety=_safety(is_single_price_auction=True),
    )

    assert facts.entry_allowed is False
    assert "KRX_SINGLE_PRICE_AUCTION" in facts.blockers


def test_clean_krx_market_safety_allows_entry() -> None:
    facts = evaluate_session(_krx_calendar(), KRX_MIDSESSION, market_safety=_safety())

    assert facts.entry_allowed is True
    assert facts.blockers == ()


def test_market_safety_is_not_required_outside_krx() -> None:
    facts = evaluate_session(_us_calendar(), OPEN + timedelta(minutes=30))

    assert facts.entry_allowed is True
    assert facts.blockers == ()
