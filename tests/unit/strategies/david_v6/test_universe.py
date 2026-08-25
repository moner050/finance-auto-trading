from __future__ import annotations

from decimal import Decimal

import pytest

from autotrader.strategies.david_v6.universe import (
    UniverseFacts,
    evaluate_cash_universe,
)


def _evaluate(**changes: object) -> UniverseFacts:
    values: dict[str, object] = {
        "member_as_of": True,
        "common_stock_as_of": True,
        "median_value_20d": Decimal("100"),
        "cross_section_median_value_20d": Decimal("100"),
        "sector_return_70d_rank": 3,
        "sector_classification": "technology",
    }
    values.update(changes)
    return evaluate_cash_universe(**values)  # type: ignore[arg-type]


def test_point_in_time_member_common_stock_and_boundary_liquidity_are_required() -> (
    None
):
    assert _evaluate().eligible is True
    assert _evaluate(member_as_of=False).blockers == ("NOT_MEMBER_AS_OF",)
    assert _evaluate(common_stock_as_of=False).blockers == ("NOT_COMMON_STOCK_AS_OF",)
    assert _evaluate(median_value_20d=Decimal("99.99")).blockers == (
        "BELOW_MEDIAN_TRADED_VALUE",
    )


def test_only_top_three_non_excluded_authoritative_sectors_are_eligible() -> None:
    assert _evaluate(sector_return_70d_rank=4).blockers == ("SECTOR_OUTSIDE_TOP_THREE",)
    assert _evaluate(sector_classification=None).blockers == (
        "SECTOR_AUTHORITY_UNAVAILABLE",
    )


@pytest.mark.parametrize("sector", ("real_estate", "financials", "energy"))
def test_excluded_sector_authorities_are_rejected(sector: str) -> None:
    facts = _evaluate(sector_classification=sector)

    assert facts.eligible is False
    assert facts.blockers == ("EXCLUDED_SECTOR",)
