from __future__ import annotations

from autotrader.strategies.david_v6.grading import (
    ABNORMAL_SPREAD_OR_SLIPPAGE,
    BLOCKING_BIG_TRADE_AHEAD,
    CANDIDATE_SCORE,
    DIRECTION_LONG,
    DIRECTION_SHORT,
    FIBONACCI_EXTENSION_CLUSTER,
    HIDDEN_DIVERGENCE,
    HIGH_CONFIDENCE_SCORE,
    HIGH_IMPACT_NEWS_RISK,
    HIGHER_TIMEFRAME_BIAS,
    PROFILE_VALUE_CONFLUENCE,
    REGULAR_HLIT_DIVERGENCE,
    SUPPORTING_BIG_TRADE_BEHIND,
    V1_CERO_OSMOTICO,
    V1_MIG_REVERSAL,
    V1_SECADO,
    grade_setup,
    indicator_weight,
    score_indicators,
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


def _indicators(*codes: str) -> tuple[MatchedIndicator, ...]:
    return tuple(_indicator(code) for code in codes)


def test_weights_match_the_specification_score_table() -> None:
    assert indicator_weight(HIGHER_TIMEFRAME_BIAS) == 2
    assert indicator_weight(REGULAR_HLIT_DIVERGENCE) == 2
    assert indicator_weight(HIDDEN_DIVERGENCE) == 1
    assert indicator_weight(FIBONACCI_EXTENSION_CLUSTER) == 2
    assert indicator_weight(PROFILE_VALUE_CONFLUENCE) == 1
    assert indicator_weight(V1_SECADO) == 2
    assert indicator_weight(V1_MIG_REVERSAL) == 2
    assert indicator_weight(SUPPORTING_BIG_TRADE_BEHIND) == 1
    assert indicator_weight(BLOCKING_BIG_TRADE_AHEAD) == -4
    assert indicator_weight(HIGH_IMPACT_NEWS_RISK) == -3
    assert indicator_weight(ABNORMAL_SPREAD_OR_SLIPPAGE) == -2


def test_regular_divergence_outweighs_hidden() -> None:
    assert indicator_weight(REGULAR_HLIT_DIVERGENCE) > indicator_weight(
        HIDDEN_DIVERGENCE
    )


def test_osmotic_zero_stays_at_telemetry_authority() -> None:
    """Section 18.2 records it but forbids it from moving a decision."""
    assert indicator_weight(V1_CERO_OSMOTICO) == 0
    assert score_indicators(_indicators(V1_CERO_OSMOTICO)) == 0


def test_unknown_indicator_carries_no_weight() -> None:
    assert indicator_weight("something-invented") == 0


def test_cutoffs_are_seven_and_nine() -> None:
    assert CANDIDATE_SCORE == 7
    assert HIGH_CONFIDENCE_SCORE == 9


def test_counting_indicators_is_not_scoring_them() -> None:
    """Nine weightless indicators must not reach the A grade."""
    weightless = _indicators(*(f"technical-{index:02d}" for index in range(9)))

    assert score_indicators(weightless) == 0
    assert grade_setup(weightless, mandatory_codes=frozenset()) is SetupGrade.NORMAL


def test_score_of_seven_is_a_candidate() -> None:
    indicators = _indicators(
        HIGHER_TIMEFRAME_BIAS,
        REGULAR_HLIT_DIVERGENCE,
        FIBONACCI_EXTENSION_CLUSTER,
        PROFILE_VALUE_CONFLUENCE,
    )

    assert score_indicators(indicators) == 7
    assert (
        grade_setup(indicators, mandatory_codes=frozenset()) is SetupGrade.A_CANDIDATE
    )


def test_score_of_nine_is_the_a_grade() -> None:
    indicators = _indicators(
        HIGHER_TIMEFRAME_BIAS,
        REGULAR_HLIT_DIVERGENCE,
        FIBONACCI_EXTENSION_CLUSTER,
        V1_SECADO,
        PROFILE_VALUE_CONFLUENCE,
    )

    assert score_indicators(indicators) == 9
    assert grade_setup(indicators, mandatory_codes=frozenset()) is SetupGrade.A


def test_blocking_big_trade_subtracts_four_and_demotes() -> None:
    strong = _indicators(
        HIGHER_TIMEFRAME_BIAS,
        REGULAR_HLIT_DIVERGENCE,
        FIBONACCI_EXTENSION_CLUSTER,
        V1_SECADO,
        PROFILE_VALUE_CONFLUENCE,
    )
    penalised = (*strong, _indicator(BLOCKING_BIG_TRADE_AHEAD))

    assert score_indicators(penalised) == 5
    assert grade_setup(penalised, mandatory_codes=frozenset()) is SetupGrade.NORMAL


def test_news_risk_and_abnormal_spread_subtract() -> None:
    indicators = _indicators(
        HIGHER_TIMEFRAME_BIAS,
        REGULAR_HLIT_DIVERGENCE,
        HIGH_IMPACT_NEWS_RISK,
        ABNORMAL_SPREAD_OR_SLIPPAGE,
    )

    assert score_indicators(indicators) == -1


def test_monday_penalty_can_demote_a_boundary_score() -> None:
    indicators = _indicators(
        HIGHER_TIMEFRAME_BIAS,
        REGULAR_HLIT_DIVERGENCE,
        FIBONACCI_EXTENSION_CLUSTER,
        PROFILE_VALUE_CONFLUENCE,
    )

    assert (
        grade_setup(indicators, mandatory_codes=frozenset()) is SetupGrade.A_CANDIDATE
    )
    assert (
        grade_setup(indicators, mandatory_codes=frozenset(), score_adjustment=-1)
        is SetupGrade.NORMAL
    )


def test_missing_mandatory_condition_demotes_to_normal() -> None:
    indicators = _indicators(
        HIGHER_TIMEFRAME_BIAS,
        REGULAR_HLIT_DIVERGENCE,
        FIBONACCI_EXTENSION_CLUSTER,
        V1_SECADO,
        PROFILE_VALUE_CONFLUENCE,
    )

    assert (
        grade_setup(indicators, mandatory_codes=frozenset({"missing"}))
        is SetupGrade.NORMAL
    )


def test_duplicate_and_unavailable_indicators_cannot_increase_the_score() -> None:
    available = _indicators(HIGHER_TIMEFRAME_BIAS, REGULAR_HLIT_DIVERGENCE)
    unavailable = _indicator(V1_SECADO)
    object.__setattr__(unavailable, "evidence_state", EvidenceState.STALE)

    actual = score_indicators((*available, available[0], unavailable))

    assert actual == 4


def test_contradictory_directions_reject() -> None:
    indicators = _indicators(
        HIGHER_TIMEFRAME_BIAS,
        REGULAR_HLIT_DIVERGENCE,
        DIRECTION_LONG,
        DIRECTION_SHORT,
    )

    assert grade_setup(indicators, mandatory_codes=frozenset()) is SetupGrade.REJECT


def test_direction_markers_carry_no_score() -> None:
    assert indicator_weight(DIRECTION_LONG) == 0
    assert indicator_weight(DIRECTION_SHORT) == 0
