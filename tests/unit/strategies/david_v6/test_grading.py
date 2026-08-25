from __future__ import annotations

from autotrader.strategies.david_v6.grading import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
    grade_setup,
)
from autotrader.strategies.david_v6.models import (
    EvidenceState,
    MatchedIndicator,
    SetupGrade,
)


def _indicator(code: str) -> MatchedIndicator:
    return MatchedIndicator(
        key=code,
        mandatory=False,
        evidence_state=EvidenceState.AVAILABLE,
        evidence_hash=code.encode().ljust(32, b"0")[:32],
    )


def _indicators(count: int) -> tuple[MatchedIndicator, ...]:
    return tuple(_indicator(f"technical-{index:02d}") for index in range(count))


def test_grade_boundaries_are_indicator_counts() -> None:
    mandatory = frozenset({"technical-00", "technical-01"})

    assert grade_setup(_indicators(0), mandatory_codes=mandatory) is SetupGrade.NORMAL
    assert grade_setup(_indicators(6), mandatory_codes=mandatory) is SetupGrade.NORMAL
    assert (
        grade_setup(_indicators(7), mandatory_codes=mandatory) is SetupGrade.A_CANDIDATE
    )
    assert (
        grade_setup(_indicators(8), mandatory_codes=mandatory) is SetupGrade.A_CANDIDATE
    )
    assert grade_setup(_indicators(9), mandatory_codes=mandatory) is SetupGrade.A
    assert grade_setup(_indicators(12), mandatory_codes=mandatory) is SetupGrade.A


def test_missing_mandatory_a_condition_demotes_to_normal() -> None:
    indicators = _indicators(9)

    assert (
        grade_setup(indicators, mandatory_codes=frozenset({"missing"}))
        is SetupGrade.NORMAL
    )


def test_duplicate_and_unavailable_indicators_cannot_increase_grade() -> None:
    available = _indicators(6)
    unavailable = _indicator("technical-06")
    object.__setattr__(unavailable, "evidence_state", EvidenceState.STALE)

    actual = grade_setup(
        (*available, available[0], unavailable),
        mandatory_codes=frozenset(),
    )

    assert actual is SetupGrade.NORMAL


def test_contradictory_directions_reject() -> None:
    indicators = (
        *_indicators(9),
        _indicator(DIRECTION_LONG),
        _indicator(DIRECTION_SHORT),
    )

    assert grade_setup(indicators, mandatory_codes=frozenset()) is SetupGrade.REJECT
