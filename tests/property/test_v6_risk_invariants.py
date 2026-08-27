from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from autotrader.domain.enums import Side
from autotrader.persistence.mysql.repositories.david_v6_risk import (
    approved_v6_policy,
)
from autotrader.risk.models import V6RiskPolicySnapshot
from autotrader.risk.v6 import (
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


def _request(current_equity: Decimal, step: Decimal) -> V6RiskRequest:
    return V6RiskRequest(
        market=V6Market.BINANCE_USDM,
        grade=SetupGrade.NORMAL,
        side=Side.BUY,
        entry_price=Decimal("100"),
        structural_reference=Decimal("99"),
        tick_size=Decimal("0.1"),
        spread=Decimal("0.1"),
        atr_30s=Decimal("2"),
        atr_5m=Decimal("2"),
        session_start_equity=Decimal("2000"),
        current_equity=current_equity,
        daily_net_pnl=Decimal("0"),
        weekly_net_pnl=Decimal("0"),
        consecutive_net_losses=0,
        current_open_structural_risk=Decimal("0"),
        quantity_step=step,
        cost_per_unit=Decimal("0.05"),
        leverage=7,
    )


@given(
    current_equity=st.decimals(
        min_value=Decimal("500"),
        max_value=Decimal("2500"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
    step=st.sampled_from((Decimal("0.001"), Decimal("0.01"), Decimal("0.1"))),
)
def test_rounded_quantity_never_exceeds_risk_budget(
    current_equity: Decimal,
    step: Decimal,
) -> None:
    authority = _authority(_request(current_equity, step))
    per_unit_loss = abs(authority.stop_price - Decimal("100")) + Decimal("0.05")

    assert authority.quantity * per_unit_loss <= authority.risk_budget


@given(
    higher=st.decimals(
        min_value=Decimal("501"),
        max_value=Decimal("2500"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
    reduction=st.decimals(
        min_value=Decimal("1"),
        max_value=Decimal("500"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
)
def test_worse_equity_cannot_increase_quantity(
    higher: Decimal,
    reduction: Decimal,
) -> None:
    lower = max(Decimal("1"), higher - reduction)
    request = _request(higher, Decimal("0.001"))

    higher_authority = _authority(request)
    lower_authority = _authority(replace(request, current_equity=lower))

    assert lower_authority.risk_base <= higher_authority.risk_base
    assert lower_authority.quantity <= higher_authority.quantity
