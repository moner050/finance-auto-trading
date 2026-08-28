"""Counting how much of the market went up.

Breadth is a share, and a share can be made to say almost anything by choosing
what goes in the denominator and which days count. These pin those choices.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from autotrader.integrations.market_data.binance_breadth import (
    BreadthReading,
    breadth_percentile,
    breadth_series,
)

DAY = date(2026, 8, 26)
NEXT = date(2026, 8, 27)


def _reading(advancing: int, declining: int, *, day: date = DAY) -> BreadthReading:
    return BreadthReading(
        exchange_date=day, advancing=advancing, declining=declining, unchanged=0
    )


def test_a_rising_close_advances_and_a_falling_one_declines() -> None:
    series = breadth_series(
        {
            "AUSDT": ((DAY, Decimal("1")), (NEXT, Decimal("2"))),
            "BUSDT": ((DAY, Decimal("2")), (NEXT, Decimal("1"))),
        }
    )

    assert len(series) == 1
    assert (series[0].advancing, series[0].declining) == (1, 1)


def test_an_unchanged_close_stays_in_the_denominator() -> None:
    """A day where nothing moved is a day nothing advanced. Dropping the flat
    ones would report it as though the market had risen."""
    reading = BreadthReading(exchange_date=DAY, advancing=1, declining=0, unchanged=1)

    assert reading.constituents == 2
    assert reading.share_advancing == Decimal("0.5")


def test_a_gap_in_a_contracts_history_is_not_a_price_move() -> None:
    """Comparing against whatever close it last happened to have would turn
    missing data into a rally."""
    series = breadth_series(
        {
            "AUSDT": (
                (date(2026, 8, 20), Decimal("1")),
                # Six days later. Not consecutive, so it contributes nothing.
                (NEXT, Decimal("5")),
            )
        }
    )

    assert series == ()


def test_a_contract_with_one_close_contributes_nothing() -> None:
    series = breadth_series({"AUSDT": ((NEXT, Decimal("1")),)})

    assert series == ()


def test_a_day_with_no_constituents_has_no_breadth() -> None:
    empty = BreadthReading(exchange_date=DAY, advancing=0, declining=0, unchanged=0)

    with pytest.raises(ValueError, match="no constituents"):
        _ = empty.share_advancing


def test_the_percentile_ranks_today_against_its_own_history() -> None:
    """A raw share of 0.4 means nothing without knowing whether 0.4 is
    unusual for this market."""
    history = [
        _reading(10, 90, day=date(2026, 8, 20)),
        _reading(50, 50, day=date(2026, 8, 21)),
        _reading(90, 10, day=date(2026, 8, 22)),
        # Today: the lowest breadth of the four.
        _reading(5, 95, day=date(2026, 8, 23)),
    ]

    assert breadth_percentile(history, minimum_constituents=10) == Decimal("0.25")


def test_a_thin_day_is_not_counted() -> None:
    """A share over eleven contracts is a different measurement from one over
    five hundred, not a noisier version of it."""
    history = [
        _reading(50, 50, day=date(2026, 8, 20)),
        _reading(60, 40, day=date(2026, 8, 21)),
        # Three contracts. Excluded, so it cannot become today's reading.
        BreadthReading(
            exchange_date=date(2026, 8, 22), advancing=3, declining=0, unchanged=0
        ),
    ]

    # Ranked among the two usable days, where the newest is the higher.
    assert breadth_percentile(history, minimum_constituents=10) == Decimal("1")


def test_a_history_of_one_day_is_not_a_percentile() -> None:
    with pytest.raises(ValueError, match="needs a history"):
        breadth_percentile([_reading(50, 50)], minimum_constituents=10)


def test_readings_come_back_in_date_order() -> None:
    series = breadth_series(
        {
            "AUSDT": (
                (date(2026, 8, 24), Decimal("1")),
                (date(2026, 8, 25), Decimal("2")),
                (date(2026, 8, 26), Decimal("3")),
            )
        }
    )

    assert [item.exchange_date for item in series] == [
        date(2026, 8, 25),
        date(2026, 8, 26),
    ]


def test_a_negative_count_is_refused() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        BreadthReading(exchange_date=DAY, advancing=-1, declining=0, unchanged=0)
