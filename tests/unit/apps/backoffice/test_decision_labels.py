"""The label table is not allowed to fall behind the codes it describes.

A translation table is the same shape as every other defect in this codebase:
two sides that have to agree, and nothing between them. A blocker added to the
engine next month would render as a bare identifier on the one screen an
operator uses to decide whether the loop is behaving - and nothing would fail.

So this reads the engine's own source for the reasons it can append, and
fails on any that has no entry. The fallback is still there and still
correct; this is what keeps it from being the normal case.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from autotrader.apps.backoffice.decision_labels import (
    BLOCKER_LABELS,
    INDICATOR_LABELS,
    blocker_label,
    indicator_label,
)

SOURCE = Path(__file__).resolve().parents[4] / "src" / "autotrader"

# `blockers.append("X")`, and the engine's checks table, which names the
# blocker as the second element of a tuple.
_APPENDED = re.compile(r'blockers\.append\(\s*"([A-Z0-9_]+)"')
_CHECK_TABLE = re.compile(r'\(\s*"[a-z_]+",\s*"([A-Z0-9_]+)",\s*(?:True|False)\s*\)')
# What an evidence item reports when it has nothing to give.
_EVIDENCE = re.compile(r'_unavailable\(\s*"([A-Z0-9_]+)"|blocker_code="([A-Z0-9_]+)"')


def _emitted() -> set[str]:
    found: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        found.update(_APPENDED.findall(text))
        found.update(_CHECK_TABLE.findall(text))
        for first, second in _EVIDENCE.findall(text):
            found.add(first or second)
    return found


def test_every_reason_the_engine_can_give_has_a_label() -> None:
    missing = sorted(_emitted() - set(BLOCKER_LABELS))

    assert not missing, f"no Korean label for: {missing}"


def test_the_scan_actually_finds_the_codes() -> None:
    """A source scan that matched nothing would pass the test above without
    checking anything."""
    emitted = _emitted()

    assert len(emitted) > 60
    # The first version of this scan used `[A-Z_]+` and could not see a code
    # with a digit in it, so four sizing reasons were silently exempt from
    # the check above - and one of them reached the screen untranslated the
    # first time a confirmed exhaustion produced a stop distance.
    assert "STOP_DISTANCE_BELOW_0_40_ATR5M" in emitted
    assert "REGULAR_DIVERGENCE_ABSENT" in emitted
    assert "CALENDAR_BLOCKED" in emitted, "the checks table is read too"
    assert "CALENDAR_UNAVAILABLE" in emitted, "evidence blockers are read too"


def test_every_weighted_indicator_has_a_label() -> None:
    from autotrader.strategies.david_v6.grading import _RESEARCH_WEIGHTS

    missing = sorted(set(_RESEARCH_WEIGHTS) - set(INDICATOR_LABELS))

    assert not missing, f"no Korean label for: {missing}"


def test_an_unknown_code_is_shown_rather_than_guessed() -> None:
    """The table cannot be exhaustive - any fact may report its own reason -
    so the fallback has to be the code, not a blank and not a near match."""
    assert blocker_label("SOMETHING_ADDED_NEXT_MONTH") == "SOMETHING_ADDED_NEXT_MONTH"
    assert indicator_label("not_an_indicator") == "not_an_indicator"


def test_the_composed_families_are_read_rather_than_left_raw() -> None:
    """Three of these are built from a fact name at runtime, so they can
    never appear in the table."""
    assert blocker_label("CALENDAR_FACT_UNAVAILABLE") == "캘린더 사실 없음"
    assert blocker_label("ZONES_VALUE_INVALID") == "존 값 무효"
    assert (
        blocker_label("INDICATOR_PROVENANCE_MISSING:hidden_divergence")
        == "지표 출처 없음 (히든 다이버전스)"
    )


def test_a_composed_family_with_an_unknown_fact_falls_back() -> None:
    assert blocker_label("WHATEVER_FACT_UNAVAILABLE") == "WHATEVER_FACT_UNAVAILABLE"


def test_only_text_is_labelled() -> None:
    for call in (blocker_label, indicator_label):
        with pytest.raises(TypeError):
            call(None)  # type: ignore[arg-type]


def test_no_label_is_empty_or_the_code_itself() -> None:
    """An entry that repeats the code adds nothing and hides that it was
    never translated."""
    for code, label in BLOCKER_LABELS.items():
        assert label.strip(), code
        assert label != code, code
    for key, label in INDICATOR_LABELS.items():
        assert label.strip(), key
        assert label != key, key
