from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from autotrader.shared.decimal import require_decimal

_EXCLUDED_SECTORS = {"energy", "financial", "financials", "real_estate"}


@dataclass(frozen=True, slots=True)
class UniverseFacts:
    country_strength_confirmed: bool
    member_as_of: bool
    common_stock_as_of: bool
    median_value_20d: Decimal
    cross_section_median_value_20d: Decimal
    sector_return_70d_rank: int
    sector_classification: str | None
    eligible: bool
    blockers: tuple[str, ...]


def evaluate_cash_universe(
    *,
    country_strength_confirmed: bool,
    member_as_of: bool,
    common_stock_as_of: bool,
    median_value_20d: Decimal,
    cross_section_median_value_20d: Decimal,
    sector_return_70d_rank: int,
    sector_classification: str | None,
) -> UniverseFacts:
    if (
        type(country_strength_confirmed) is not bool
        or type(member_as_of) is not bool
        or type(common_stock_as_of) is not bool
    ):
        raise TypeError("point-in-time membership facts must be exact bool")
    median_value = require_decimal(median_value_20d)
    cross_section_median = require_decimal(cross_section_median_value_20d)
    if median_value < 0 or cross_section_median < 0:
        raise ValueError("traded-value medians must be non-negative")
    if type(sector_return_70d_rank) is not int or sector_return_70d_rank <= 0:
        raise ValueError("sector rank must be a positive integer")
    sector = _normalize_sector(sector_classification)
    blockers: list[str] = []
    # Section 2.1 filter one: the country must currently be a strong one.
    if not country_strength_confirmed:
        blockers.append("COUNTRY_NOT_STRONG")
    if not member_as_of:
        blockers.append("NOT_MEMBER_AS_OF")
    if not common_stock_as_of:
        blockers.append("NOT_COMMON_STOCK_AS_OF")
    if median_value < cross_section_median:
        blockers.append("BELOW_MEDIAN_TRADED_VALUE")
    if sector_return_70d_rank > 3:
        blockers.append("SECTOR_OUTSIDE_TOP_THREE")
    if sector is None:
        blockers.append("SECTOR_AUTHORITY_UNAVAILABLE")
    elif sector in _EXCLUDED_SECTORS:
        blockers.append("EXCLUDED_SECTOR")
    canonical_blockers = tuple(sorted(blockers))
    return UniverseFacts(
        country_strength_confirmed=country_strength_confirmed,
        member_as_of=member_as_of,
        common_stock_as_of=common_stock_as_of,
        median_value_20d=median_value,
        cross_section_median_value_20d=cross_section_median,
        sector_return_70d_rank=sector_return_70d_rank,
        sector_classification=sector,
        eligible=not canonical_blockers,
        blockers=canonical_blockers,
    )


def _normalize_sector(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value.strip() or "\r" in value or "\n" in value:
        raise ValueError("sector classification must be single-line text")
    return "_".join(value.strip().casefold().replace("-", " ").split())


__all__ = ("UniverseFacts", "evaluate_cash_universe")
