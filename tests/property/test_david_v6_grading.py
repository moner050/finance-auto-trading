from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from autotrader.strategies.david_v6.grading import grade_setup
from autotrader.strategies.david_v6.models import (
    EvidenceState,
    MatchedIndicator,
)


def _indicator(code: str) -> MatchedIndicator:
    return MatchedIndicator(
        key=code,
        mandatory=False,
        evidence_state=EvidenceState.AVAILABLE,
        evidence_hash=code.encode().ljust(32, b"0")[:32],
    )


@given(
    codes=st.lists(
        st.integers(min_value=0, max_value=15),
        min_size=1,
        max_size=30,
    ),
    order=st.data(),
)
def test_reordering_and_duplicates_cannot_change_grade(
    codes: list[int],
    order: st.DataObject,
) -> None:
    unique = tuple(sorted({f"technical-{value:02d}" for value in codes}))
    indicators = tuple(_indicator(code) for code in unique)
    permutation = order.draw(st.permutations(indicators))
    duplicated = (*permutation, *permutation)

    assert grade_setup(duplicated, mandatory_codes=frozenset()) == grade_setup(
        indicators,
        mandatory_codes=frozenset(),
    )
