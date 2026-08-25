from __future__ import annotations

from collections.abc import Sequence

from autotrader.strategies.david_v6.models import (
    EvidenceState,
    MatchedIndicator,
    SetupGrade,
)

DIRECTION_LONG = "direction:LONG"
DIRECTION_SHORT = "direction:SHORT"


def grade_setup(
    indicators: Sequence[MatchedIndicator],
    *,
    mandatory_codes: frozenset[str],
) -> SetupGrade:
    if any(type(indicator) is not MatchedIndicator for indicator in indicators):
        raise TypeError("indicators must contain exact MatchedIndicator values")
    if type(mandatory_codes) is not frozenset or any(
        type(code) is not str or not code or code.strip() != code
        for code in mandatory_codes
    ):
        raise ValueError("mandatory_codes must contain non-empty trimmed text")
    matched_codes = {
        indicator.key
        for indicator in indicators
        if indicator.evidence_state is EvidenceState.AVAILABLE
    }
    if {DIRECTION_LONG, DIRECTION_SHORT} <= matched_codes:
        return SetupGrade.REJECT
    count = len(matched_codes)
    mandatory_matched = mandatory_codes <= matched_codes
    if count >= 9 and mandatory_matched:
        return SetupGrade.A
    if count >= 7 and mandatory_matched:
        return SetupGrade.A_CANDIDATE
    return SetupGrade.NORMAL


__all__ = ("DIRECTION_LONG", "DIRECTION_SHORT", "grade_setup")
