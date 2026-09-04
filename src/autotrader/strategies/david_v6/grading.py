"""Setup grading from the section 21.3 weighted score.

One weight comes from elsewhere. Section 9.4, marked (A0), gives the
blocking Big Trade -2 where section 21.3's table gives -4, and where a
confirmed rule and a research frame disagree the confirmed one is taken.
The reasoning sits on the row itself.

The weights and the two cutoffs are transcribed from the specification, but
the specification is careful about what they are and this should be too. The
section is titled "연구용 점수표" and ends: "이 점수는 David의 직접식이 아니다
... Ablation으로 검증하기 위한 연구 프레임이다". It is a research frame, not
the author's formula, and section 15.2 classifies the Cyborg large-move
determination it stands in for as `score_only`.

So the grade is computed and recorded here, and what it is allowed to decide
is settled elsewhere: `risk.v6.RESEARCH_SCORE_AUTHORITY` holds it to a score
rather than letting it size an order.

Names of reverse-engineered items carry their V1 source, which section 18.3
requires so that a hypothesis is never mistaken for a confirmed rule.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from autotrader.strategies.david_v6.models import (
    EvidenceState,
    MatchedIndicator,
    SetupGrade,
)

DIRECTION_LONG = "direction:LONG"
DIRECTION_SHORT = "direction:SHORT"

HIGHER_TIMEFRAME_BIAS = "higher_timeframe_bias_aligned"
REGULAR_HLIT_DIVERGENCE = "regular_hlit_divergence"
HIDDEN_DIVERGENCE = "hidden_divergence"
FIBONACCI_EXTENSION_CLUSTER = "fibonacci_extension_cluster"
PROFILE_VALUE_CONFLUENCE = "profile_value_confluence"
V1_SECADO = "v1_secado"
V1_CERO_OSMOTICO = "v1_cero_osmotico"
V1_MIG_REVERSAL = "v1_mig_reversal"
SUPPORTING_BIG_TRADE_BEHIND = "supporting_big_trade_behind"
BLOCKING_BIG_TRADE_AHEAD = "blocking_big_trade_ahead"
HIGH_IMPACT_NEWS_RISK = "high_impact_news_risk"
ABNORMAL_SPREAD_OR_SLIPPAGE = "abnormal_spread_or_slippage"

_RESEARCH_WEIGHTS: Mapping[str, int] = MappingProxyType(
    {
        HIGHER_TIMEFRAME_BIAS: 2,
        REGULAR_HLIT_DIVERGENCE: 2,
        HIDDEN_DIVERGENCE: 1,
        FIBONACCI_EXTENSION_CLUSTER: 2,
        PROFILE_VALUE_CONFLUENCE: 1,
        V1_SECADO: 2,
        V1_CERO_OSMOTICO: 1,
        V1_MIG_REVERSAL: 2,
        SUPPORTING_BIG_TRADE_BEHIND: 1,
        # -2, not §21.3's -4. This is the one row that does not come from
        # that table, and §9.4 is why: it is marked (A0) - the author's own
        # use - and its `scoring:` block reads `blocking_ahead: -2` beside
        # the `behind_supporting: +1` that both sources agree on. §21.3 says
        # of itself that it is not David's formula but a research frame for
        # ablation, so where the two disagree the confirmed one is taken.
        #
        # Nothing observable changes today: §21.3 is held at SCORE_ONLY, so
        # the score decides a risk fraction tier that `_allowed` caps back to
        # the normal fraction, and it never decides whether to trade. The
        # refusal is separate and stays - §9.4 says "감점 또는 거부".
        BLOCKING_BIG_TRADE_AHEAD: -2,
        HIGH_IMPACT_NEWS_RISK: -3,
        ABNORMAL_SPREAD_OR_SLIPPAGE: -2,
    }
)

# Section 18.2 holds these at telemetry authority: recorded, but never allowed
# to move a decision until a walk-forward promotion (section 18.3, rule 5).
_TELEMETRY_ONLY = frozenset({V1_CERO_OSMOTICO})

# Every indicator section 15.2 lists as undisclosed and reverse-engineered:
# Secado, Cero osmótico, the reversal MIG, and the two that depend on a Big
# Trades threshold the author never published. All are `score_only` there.
#
# The document's governing principle is that confirmed and estimated rules are
# never mixed, and that an estimated one gets no order authority until it has
# passed backtest and shadow trading. A mandatory indicator is order authority
# in its strongest form - without it there is no entry at all - so a hypothesis
# may contribute weight here and may not be required.
HYPOTHESIS_CODES = frozenset(
    {
        V1_SECADO,
        V1_CERO_OSMOTICO,
        V1_MIG_REVERSAL,
        SUPPORTING_BIG_TRADE_BEHIND,
        BLOCKING_BIG_TRADE_AHEAD,
    }
)

CANDIDATE_SCORE = 7
HIGH_CONFIDENCE_SCORE = 9


def indicator_weight(key: str) -> int:
    """Decision weight of one indicator, zero when it carries none."""
    if type(key) is not str:
        raise TypeError("indicator key must be text")
    if key in _TELEMETRY_ONLY:
        return 0
    return _RESEARCH_WEIGHTS.get(key, 0)


def score_indicators(indicators: Sequence[MatchedIndicator]) -> int:
    """Sum the section 21.3 weights over distinct available indicators."""
    return sum(indicator_weight(key) for key in _matched_codes(indicators))


def grade_setup(
    indicators: Sequence[MatchedIndicator],
    *,
    mandatory_codes: frozenset[str],
    score_adjustment: int = 0,
) -> SetupGrade:
    if any(type(indicator) is not MatchedIndicator for indicator in indicators):
        raise TypeError("indicators must contain exact MatchedIndicator values")
    if type(mandatory_codes) is not frozenset or any(
        type(code) is not str or not code or code.strip() != code
        for code in mandatory_codes
    ):
        raise ValueError("mandatory_codes must contain non-empty trimmed text")
    if type(score_adjustment) is not int:
        raise TypeError("score_adjustment must be an int")
    matched_codes = _matched_codes(indicators)
    if {DIRECTION_LONG, DIRECTION_SHORT} <= matched_codes:
        return SetupGrade.REJECT
    score = sum(indicator_weight(key) for key in matched_codes) + score_adjustment
    if not mandatory_codes <= matched_codes:
        return SetupGrade.NORMAL
    if score >= HIGH_CONFIDENCE_SCORE:
        return SetupGrade.A
    if score >= CANDIDATE_SCORE:
        return SetupGrade.A_CANDIDATE
    return SetupGrade.NORMAL


def _matched_codes(indicators: Sequence[MatchedIndicator]) -> set[str]:
    return {
        indicator.key
        for indicator in indicators
        if indicator.evidence_state is EvidenceState.AVAILABLE
    }


__all__ = (
    "ABNORMAL_SPREAD_OR_SLIPPAGE",
    "BLOCKING_BIG_TRADE_AHEAD",
    "CANDIDATE_SCORE",
    "DIRECTION_LONG",
    "DIRECTION_SHORT",
    "FIBONACCI_EXTENSION_CLUSTER",
    "HIDDEN_DIVERGENCE",
    "HIGHER_TIMEFRAME_BIAS",
    "HIGH_CONFIDENCE_SCORE",
    "HIGH_IMPACT_NEWS_RISK",
    "HYPOTHESIS_CODES",
    "PROFILE_VALUE_CONFLUENCE",
    "REGULAR_HLIT_DIVERGENCE",
    "SUPPORTING_BIG_TRADE_BEHIND",
    "V1_CERO_OSMOTICO",
    "V1_MIG_REVERSAL",
    "V1_SECADO",
    "grade_setup",
    "indicator_weight",
    "score_indicators",
)
