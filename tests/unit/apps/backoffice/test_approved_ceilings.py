"""The approved figures the policy screen shows.

Section 11.4 asks for the approved absolute and percentage limits on the page.
The point of showing them is that an operator can tell what a policy version
could never do, so each one has to come from the code that enforces it rather
than from a number typed into a template.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autotrader.apps.backoffice.policies_read_model import approved_ceilings
from autotrader.persistence.mysql.repositories.david_v6_risk import (
    APPROVED_V6_RISK_POLICIES,
)
from autotrader.risk.models import TRADE_RISK_CEILING, V6RiskPolicySnapshot
from autotrader.risk.v6 import (
    APPROVED_CAPITAL,
    MAX_LEVERAGE,
    SESSION_TRADE_UPPER_BOUND,
)
from autotrader.shared.ids import new_uuid7
from autotrader.strategies.david_v6.models import V6Market


def test_the_ceilings_come_from_the_engine() -> None:
    ceilings = approved_ceilings()

    assert ceilings.max_leverage == MAX_LEVERAGE
    assert ceilings.session_trade_upper_bound == SESSION_TRADE_UPPER_BOUND
    assert ceilings.trade_risk_fraction == TRADE_RISK_CEILING


def test_the_trade_risk_ceiling_reads_as_one_percent() -> None:
    assert approved_ceilings().trade_risk_percentage == "1%"


def test_every_market_has_an_approved_capital_figure() -> None:
    markets = {item.market for item in approved_ceilings().capital}

    assert markets == {market.value for market in V6Market}


@pytest.mark.parametrize(
    ("market", "amount", "unit"),
    (
        (V6Market.KRX_CASH, "1,000,000", "KRW"),
        (V6Market.US_CASH, "2,000", "USD"),
        (V6Market.BINANCE_USDM, "2,000", "USDT"),
    ),
)
def test_the_approved_amounts_are_grouped_and_whole(
    market: V6Market, amount: str, unit: str
) -> None:
    """1,000,000 and 100,000 are one glance apart on a screen."""
    item = next(
        view for view in approved_ceilings().capital if view.market == market.value
    )

    assert (item.amount, item.unit) == (amount, unit)


def test_no_approved_policy_sits_above_the_trade_risk_ceiling() -> None:
    """Otherwise the screen would show a ceiling the engine had already let
    a policy through."""
    assert all(
        item.absolute_trade_risk_fraction <= TRADE_RISK_CEILING
        for item in APPROVED_V6_RISK_POLICIES
    )


def test_a_policy_above_the_ceiling_is_refused() -> None:
    definition = next(
        item
        for item in APPROVED_V6_RISK_POLICIES
        if item.code == "DAVID_V6_BINANCE_USDM_USDT"
    )
    snapshot = definition.snapshot(new_uuid7())

    with pytest.raises(ValueError, match="one percent"):
        _above_the_ceiling(snapshot)


def _above_the_ceiling(snapshot: V6RiskPolicySnapshot) -> V6RiskPolicySnapshot:
    # Every grade moves with it, since a grade may not exceed the absolute
    # fraction and the constructor checks that first.
    raised = Decimal("0.02")
    return replace(
        snapshot,
        absolute_trade_risk_fraction=raised,
        normal_risk_fraction=raised,
        a_candidate_risk_fraction=raised,
        a_risk_fraction=raised,
    )


def test_the_capital_figures_are_the_ones_in_the_code() -> None:
    """A second copy would drift from the first one silently."""
    assert {(item.market, item.amount, item.unit) for item in APPROVED_CAPITAL} == {
        (V6Market.KRX_CASH, Decimal("1000000"), "KRW"),
        (V6Market.US_CASH, Decimal("2000"), "USD"),
        (V6Market.BINANCE_USDM, Decimal("2000"), "USDT"),
    }
