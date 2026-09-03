from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autotrader.domain.enums import Side
from autotrader.persistence.mysql.repositories.david_v6_risk import (
    approved_v6_policy,
)
from autotrader.risk.models import V6RiskPolicySnapshot
from autotrader.risk.v6 import (
    RESEARCH_SCORE_AUTHORITY,
    SCORE_ONLY,
    V6RiskAuthority,
    V6RiskRequest,
    evaluate_v6_risk,
)
from autotrader.shared.ids import new_uuid7
from autotrader.strategies.david_v6.models import SetupGrade, V6Market


def _policy(market: V6Market = V6Market.BINANCE_USDM) -> V6RiskPolicySnapshot:
    """The approved policy, which is what the engine sizes against."""
    return approved_v6_policy(market, policy_version_id=new_uuid7())


def _authority(request: V6RiskRequest) -> V6RiskAuthority:
    """Evaluate against the approved policy for the market the request names.

    Taken from the request rather than passed alongside it, because the two
    disagreeing is exactly what the engine refuses.
    """
    return evaluate_v6_risk(request, policy=_policy(request.market))


def _request(**changes: object) -> V6RiskRequest:
    values: dict[str, object] = {
        "market": V6Market.BINANCE_USDM,
        "grade": SetupGrade.NORMAL,
        "side": Side.BUY,
        "entry_price": Decimal("100"),
        "structural_reference": Decimal("99"),
        "tick_size": Decimal("0.1"),
        "spread": Decimal("0.1"),
        "atr_30s": Decimal("2"),
        "atr_5m": Decimal("2"),
        "session_start_equity": Decimal("2000"),
        "current_equity": Decimal("1800"),
        "daily_net_pnl": Decimal("0"),
        "weekly_net_pnl": Decimal("0"),
        "consecutive_net_losses": 0,
        "current_open_structural_risk": Decimal("0"),
        "quantity_step": Decimal("0.001"),
        "cost_per_unit": Decimal("0.05"),
        "leverage": 7,
    }
    values.update(changes)
    return V6RiskRequest(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("side", "reference", "expected_stop"),
    (
        (Side.BUY, Decimal("99"), Decimal("98.6")),
        (Side.SELL, Decimal("101"), Decimal("101.4")),
    ),
)
def test_structural_stop_uses_largest_required_buffer(
    side: Side,
    reference: Decimal,
    expected_stop: Decimal,
) -> None:
    authority = _authority(_request(side=side, structural_reference=reference))

    assert authority.allowed is True
    assert authority.stop_price == expected_stop
    assert authority.stop_distance_atr5m == Decimal("0.7")


def test_stop_distance_must_be_between_point_four_and_one_point_five_atr() -> None:
    too_tight = _authority(_request(structural_reference=Decimal("99.7")))
    too_wide = _authority(_request(structural_reference=Decimal("96.5")))

    assert "STOP_DISTANCE_BELOW_0_40_ATR5M" in too_tight.blocker_codes
    assert "STOP_DISTANCE_ABOVE_1_50_ATR5M" in too_wide.blocker_codes


@pytest.mark.parametrize(
    ("market", "grade", "expected"),
    (
        (V6Market.KRX_CASH, SetupGrade.NORMAL, Decimal("0.0015")),
        # Lowered to section 21's stated `per_trade` when the policy stopped
        # running above the only figures the document gives.
        (V6Market.BINANCE_USDM, SetupGrade.NORMAL, Decimal("0.0015")),
        (V6Market.BINANCE_USDM, SetupGrade.A_CANDIDATE, Decimal("0.0015")),
        # A drew 0.0050 until the score that produces the grade was held to
        # what section 21.3 says it is. See the test below.
        (V6Market.US_CASH, SetupGrade.A, Decimal("0.0015")),
        (V6Market.BINANCE_USDM, SetupGrade.A, Decimal("0.0015")),
    ),
)
def test_grade_and_market_select_fixed_percentage_risk(
    market: V6Market,
    grade: SetupGrade,
    expected: Decimal,
) -> None:
    authority = _authority(
        _request(
            market=market,
            grade=grade,
            atr_30s=(None if market is not V6Market.BINANCE_USDM else Decimal("2")),
            leverage=(None if market is not V6Market.BINANCE_USDM else 7),
        )
    )

    assert authority.risk_fraction == expected
    assert authority.risk_base == Decimal("1800")
    assert authority.risk_budget == Decimal("1800") * expected


def test_a_research_score_does_not_enlarge_a_position() -> None:
    """Section 21.3 titles itself 연구용 점수표 and says "이 점수는 David의
    직접식이 아니다"; section 15.2 marks the determination it stands in for as
    `score_only`. Sizing is order authority, and the document's rule is that
    an estimated rule gets none until it has passed backtest and shadow."""
    assert RESEARCH_SCORE_AUTHORITY == SCORE_ONLY

    graded = _authority(
        _request(
            market=V6Market.BINANCE_USDM,
            grade=SetupGrade.A,
            atr_30s=Decimal("2"),
            leverage=7,
        )
    )
    plain = _authority(
        _request(
            market=V6Market.BINANCE_USDM,
            grade=SetupGrade.NORMAL,
            atr_30s=Decimal("2"),
            leverage=7,
        )
    )

    assert graded.risk_fraction == plain.risk_fraction


def test_holding_the_score_down_never_turns_a_refusal_into_a_trade() -> None:
    """Only the size is capped. A cash policy carries no A-candidate fraction,
    and that absence must stay a refusal rather than become a smaller order."""
    blocked = _authority(
        _request(market=V6Market.US_CASH, grade=SetupGrade.A_CANDIDATE)
    )

    assert "CASH_A_CANDIDATE_UNSUPPORTED" in blocked.blocker_codes
    assert blocked.risk_fraction == Decimal(0)


def test_cash_candidate_and_missing_binance_atr_are_blocked() -> None:
    cash_candidate = _authority(
        _request(
            market=V6Market.US_CASH,
            grade=SetupGrade.A_CANDIDATE,
            atr_30s=None,
            leverage=None,
        )
    )
    missing_atr = _authority(_request(atr_30s=None))

    assert "CASH_A_CANDIDATE_UNSUPPORTED" in cash_candidate.blocker_codes
    assert "BINANCE_ATR30S_REQUIRED" in missing_atr.blocker_codes


def test_quantity_rounds_down_and_zero_quantity_rejects() -> None:
    authority = _authority(_request(quantity_step=Decimal("0.01")))
    zero = _authority(
        _request(quantity_step=Decimal("10"), current_equity=Decimal("100"))
    )

    per_unit_loss = abs(authority.stop_price - Decimal("100")) + Decimal("0.05")
    assert authority.quantity % Decimal("0.01") == 0
    assert authority.quantity * per_unit_loss <= authority.risk_budget
    assert zero.quantity == 0
    assert "ROUNDED_QUANTITY_ZERO" in zero.blocker_codes


@pytest.mark.parametrize(
    ("changes", "blocker"),
    (
        ({"daily_net_pnl": Decimal("-13.5")}, "DAILY_LOSS_LIMIT"),
        ({"weekly_net_pnl": Decimal("-36")}, "WEEKLY_LOSS_LIMIT"),
        ({"consecutive_net_losses": 2}, "CONSECUTIVE_LOSS_LIMIT"),
        # The gate trips when open risk plus this trade's budget exceeds the
        # ceiling. A smaller budget needs more already open to reach it.
        ({"current_open_structural_risk": Decimal("12.81")}, "OPEN_RISK_LIMIT"),
        ({"leverage": 8}, "BINANCE_LEVERAGE_LIMIT"),
    ),
)
def test_loss_open_risk_and_leverage_gates_fail_closed(
    changes: dict[str, object],
    blocker: str,
) -> None:
    authority = _authority(_request(**changes))

    assert authority.allowed is False
    assert blocker in authority.blocker_codes


def test_worse_current_equity_is_the_risk_base() -> None:
    request = _request(current_equity=Decimal("2200"))
    higher = _authority(request)
    lower = _authority(replace(request, current_equity=Decimal("1500")))

    assert higher.risk_base == Decimal("2000")
    assert lower.risk_base == Decimal("1500")
    assert lower.quantity <= higher.quantity


def test_spread_wider_than_three_ticks_is_blocked() -> None:
    authority = _authority(_request(tick_size=Decimal("0.1"), spread=Decimal("0.31")))

    assert "SPREAD_ABOVE_THREE_TICKS" in authority.blocker_codes
    assert authority.quantity == Decimal(0)


def test_spread_of_exactly_three_ticks_is_allowed() -> None:
    authority = _authority(_request(tick_size=Decimal("0.1"), spread=Decimal("0.30")))

    assert "SPREAD_ABOVE_THREE_TICKS" not in authority.blocker_codes


def test_size_multiplier_scales_the_quantity() -> None:
    full = _authority(_request())
    halved = _authority(_request(size_multiplier=Decimal("0.5")))

    # The halved size is floored to the quantity step, never rounded up.
    # Both fell with the risk fraction: 0.0025 -> 0.0015 is 0.6x.
    assert halved.quantity == Decimal("0.931")
    assert full.quantity == Decimal("1.862")
    assert halved.quantity <= full.quantity / 2
    assert halved.risk_fraction == full.risk_fraction


def test_max_quantity_caps_the_size() -> None:
    authority = _authority(_request(max_quantity=Decimal("0.5")))

    assert authority.quantity == Decimal("0.5")


def test_size_multiplier_must_be_within_zero_and_one() -> None:
    for invalid in (Decimal(0), Decimal("1.5"), Decimal("-1")):
        with pytest.raises(ValueError, match="size_multiplier"):
            _request(size_multiplier=invalid)


def test_session_trade_count_below_the_bound_is_allowed() -> None:
    """Section 6 refuses a fixed daily count, so seven trades must pass."""
    authority = _authority(_request(session_trade_count=7))

    assert "SESSION_TRADE_UPPER_BOUND" not in authority.blocker_codes
    assert authority.quantity > Decimal(0)


def test_the_eighth_trade_of_a_session_is_blocked() -> None:
    authority = _authority(_request(session_trade_count=8))

    assert "SESSION_TRADE_UPPER_BOUND" in authority.blocker_codes
    assert authority.quantity == Decimal(0)


def test_session_trade_count_must_be_a_non_negative_integer() -> None:
    with pytest.raises(ValueError, match="session_trade_count"):
        _request(session_trade_count=-1)


def test_reaching_the_session_objective_stops_new_entries() -> None:
    authority = _authority(_request(session_objective_reached=True))

    assert "SESSION_OBJECTIVE_REACHED" in authority.blocker_codes
    assert authority.quantity == Decimal(0)


def test_an_unmet_objective_does_not_block() -> None:
    authority = _authority(_request(session_objective_reached=False))

    assert "SESSION_OBJECTIVE_REACHED" not in authority.blocker_codes


def test_a_rejected_setup_reports_its_cause_and_not_the_consequence() -> None:
    """A zero quantity says nothing SETUP_REJECTED did not already say.

    The rejected grade takes the risk fraction to zero, which takes the
    budget and the quantity to zero, and ROUNDED_QUANTITY_ZERO used to be
    appended on top. Every decision this account had ever refused carried
    both, so a screen listing reasons put a consequence at the top of the
    list beside its own cause, and the two could not be told apart.
    """
    authority = _authority(_request(grade=SetupGrade.REJECT))

    assert "SETUP_REJECTED" in authority.blocker_codes
    assert "ROUNDED_QUANTITY_ZERO" not in authority.blocker_codes
    assert not authority.allowed
    assert authority.quantity == 0


def test_a_budget_too_small_to_round_still_says_so() -> None:
    """The consequence is still reported where it is the actual finding."""
    authority = _authority(_request(quantity_step=Decimal("1000")))

    assert "ROUNDED_QUANTITY_ZERO" in authority.blocker_codes
    assert "SETUP_REJECTED" not in authority.blocker_codes
