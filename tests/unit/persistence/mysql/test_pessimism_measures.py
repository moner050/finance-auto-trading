"""Measures in, percentiles out.

The ranking is where this can quietly lie: a rank over four days looks exactly
like a rank over four hundred, and a gap filled with yesterday's number looks
exactly like an observation. Both are pinned here.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from autotrader.persistence.mysql.repositories.pessimism import (
    DailyMeasures,
    rank,
)

DAY = date(2026, 8, 26)


def _measures(**changes: object) -> DailyMeasures:
    values: dict[str, object] = {
        "exchange_date": DAY,
        "realised_volatility": Decimal("0.02"),
        "breadth_advancing": 111,
        "breadth_declining": 408,
        "breadth_unchanged": 5,
        "calls_volume": Decimal("44592.0"),
        "puts_volume": Decimal("14563.3"),
    }
    values.update(changes)
    return DailyMeasures(**values)  # type: ignore[arg-type]


def test_the_share_is_derived_from_the_counts() -> None:
    """Stored counts can be checked against something; a stored share cannot."""
    found = _measures().breadth_share

    assert found == Decimal(111) / Decimal(524)


def test_the_ratio_is_derived_from_the_volumes() -> None:
    assert _measures().put_call_ratio == Decimal("14563.3") / Decimal("44592.0")


def test_breadth_is_three_counts_or_none_of_them() -> None:
    """Two thirds of a count is not a smaller measurement, it is an unusable
    one."""
    with pytest.raises(ValueError, match="three counts or none"):
        _measures(breadth_unchanged=None)


def test_a_put_call_reading_is_both_volumes_or_neither() -> None:
    with pytest.raises(ValueError, match="both volumes or neither"):
        _measures(puts_volume=None)


def test_a_day_that_observed_nothing_is_not_a_measurement() -> None:
    with pytest.raises(ValueError, match="observed nothing"):
        DailyMeasures(exchange_date=DAY)


def test_a_day_with_no_call_volume_has_no_ratio() -> None:
    assert _measures(calls_volume=Decimal(0)).put_call_ratio is None


def test_a_day_where_only_one_venue_answered_is_still_a_measurement() -> None:
    """A venue being down costs that measure and nothing else."""
    found = _measures(calls_volume=None, puts_volume=None)

    assert found.breadth_share is not None
    assert found.put_call_ratio is None


def test_the_rank_is_the_share_at_or_below_today() -> None:
    values = [Decimal(1), Decimal(2), Decimal(3), Decimal(4)]

    assert rank(values, today=Decimal(2)) == Decimal("0.5")


def test_the_lowest_value_ranks_at_the_bottom_not_at_zero() -> None:
    """Today is one of the observations. A rank of zero would say the market
    has never been this low including now, which is not what was measured."""
    values = [Decimal(1), Decimal(2), Decimal(3), Decimal(4)]

    assert rank(values, today=Decimal(1)) == Decimal("0.25")


def test_ties_count_as_at_or_below() -> None:
    values = [Decimal(1), Decimal(2), Decimal(2), Decimal(3)]

    assert rank(values, today=Decimal(2)) == Decimal("0.75")


def test_a_rank_without_values_is_refused() -> None:
    with pytest.raises(ValueError, match="needs values"):
        rank([], today=Decimal(1))
