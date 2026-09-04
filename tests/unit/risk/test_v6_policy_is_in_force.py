"""The policy decides the size, not a constant in the engine.

Before this, the fractions lived in risk/v6.py and the policy table nothing
read. These are the tests that would fail if either half drifted back.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from unit.risk.test_v6_risk import _policy, _request

from autotrader.risk.v6 import MAX_LEVERAGE, evaluate_v6_risk
from autotrader.strategies.david_v6.models import SetupGrade, V6Market


def test_a_smaller_risk_fraction_buys_less() -> None:
    policy = _policy(V6Market.BINANCE_USDM)
    request = _request()

    full = evaluate_v6_risk(request, policy=policy)
    halved = evaluate_v6_risk(
        request,
        policy=replace(policy, normal_risk_fraction=policy.normal_risk_fraction / 2),
    )

    # The number on the policy is the number that moves the money.
    assert full.quantity > halved.quantity
    assert halved.risk_fraction == policy.normal_risk_fraction / 2


def test_a_tighter_daily_loss_limit_stops_sooner() -> None:
    policy = _policy(V6Market.BINANCE_USDM)
    # A loss inside the approved limit: risk base 1800 at 0.75% is 13.50.
    request = _request(daily_net_pnl=Decimal("-10"))

    assert (
        "DAILY_LOSS_LIMIT" not in evaluate_v6_risk(request, policy=policy).blocker_codes
    )
    tightened = replace(policy, daily_loss_fraction=Decimal("0.0001"))
    assert (
        "DAILY_LOSS_LIMIT" in evaluate_v6_risk(request, policy=tightened).blocker_codes
    )


def test_a_tighter_consecutive_loss_limit_stops_sooner() -> None:
    policy = _policy(V6Market.BINANCE_USDM)
    request = _request(consecutive_net_losses=1)

    assert (
        "CONSECUTIVE_LOSS_LIMIT"
        not in evaluate_v6_risk(request, policy=policy).blocker_codes
    )
    assert (
        "CONSECUTIVE_LOSS_LIMIT"
        in evaluate_v6_risk(
            request, policy=replace(policy, max_consecutive_losses=1)
        ).blocker_codes
    )


def test_a_tighter_open_risk_limit_stops_sooner() -> None:
    policy = _policy(V6Market.BINANCE_USDM)
    request = _request()

    assert (
        "OPEN_RISK_LIMIT" not in evaluate_v6_risk(request, policy=policy).blocker_codes
    )
    assert (
        "OPEN_RISK_LIMIT"
        in evaluate_v6_risk(
            request,
            policy=replace(policy, max_open_structural_risk_fraction=Decimal("0.0001")),
        ).blocker_codes
    )


def test_a_policy_with_no_candidate_fraction_refuses_that_grade() -> None:
    """Cash carries no A-candidate fraction, and its absence is the rule."""
    cash = _policy(V6Market.US_CASH)
    request = _request(market=V6Market.US_CASH, grade=SetupGrade.A_CANDIDATE)

    authority = evaluate_v6_risk(request, policy=cash)

    assert "CASH_A_CANDIDATE_UNSUPPORTED" in authority.blocker_codes
    assert authority.quantity == 0


def test_a_policy_for_another_market_is_refused_rather_than_applied() -> None:
    """Cash fractions applied to a leveraged request would size a futures
    position against limits approved for an unleveraged one."""
    with pytest.raises(ValueError, match="same market"):
        evaluate_v6_risk(_request(), policy=_policy(V6Market.US_CASH))


def test_the_engine_will_not_size_without_a_policy() -> None:
    with pytest.raises(TypeError):
        evaluate_v6_risk(_request(), policy=None)  # type: ignore[arg-type]


def test_the_leverage_ceiling_is_not_a_policy_setting() -> None:
    """A policy row that could raise it would turn a ceiling into a default.

    The number itself is the operator's and has moved once already, from seven
    to fifty on 2026-09-04, so this asserts the shape rather than the value:
    one over the ceiling is refused, and no policy field can change where the
    ceiling is.
    """
    policy = _policy(V6Market.BINANCE_USDM)

    assert MAX_LEVERAGE > 0
    assert (
        "BINANCE_LEVERAGE_LIMIT"
        in evaluate_v6_risk(
            _request(leverage=MAX_LEVERAGE + 1), policy=policy
        ).blocker_codes
    )
    # There is no field on the policy that could change that.
    assert not hasattr(policy, "max_leverage")
