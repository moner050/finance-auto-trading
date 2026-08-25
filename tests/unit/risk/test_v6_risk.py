from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autotrader.domain.enums import Side
from autotrader.risk.v6 import V6RiskRequest, evaluate_v6_risk
from autotrader.strategies.david_v6.models import SetupGrade, V6Market


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
    authority = evaluate_v6_risk(_request(side=side, structural_reference=reference))

    assert authority.allowed is True
    assert authority.stop_price == expected_stop
    assert authority.stop_distance_atr5m == Decimal("0.7")


def test_stop_distance_must_be_between_point_four_and_one_point_five_atr() -> None:
    too_tight = evaluate_v6_risk(_request(structural_reference=Decimal("99.7")))
    too_wide = evaluate_v6_risk(_request(structural_reference=Decimal("96.5")))

    assert "STOP_DISTANCE_BELOW_0_40_ATR5M" in too_tight.blocker_codes
    assert "STOP_DISTANCE_ABOVE_1_50_ATR5M" in too_wide.blocker_codes


@pytest.mark.parametrize(
    ("market", "grade", "expected"),
    (
        (V6Market.KRX_CASH, SetupGrade.NORMAL, Decimal("0.0015")),
        (V6Market.US_CASH, SetupGrade.A, Decimal("0.0025")),
        (V6Market.BINANCE_USDM, SetupGrade.NORMAL, Decimal("0.0025")),
        (V6Market.BINANCE_USDM, SetupGrade.A_CANDIDATE, Decimal("0.0025")),
        (V6Market.BINANCE_USDM, SetupGrade.A, Decimal("0.0050")),
    ),
)
def test_grade_and_market_select_fixed_percentage_risk(
    market: V6Market,
    grade: SetupGrade,
    expected: Decimal,
) -> None:
    authority = evaluate_v6_risk(
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


def test_cash_candidate_and_missing_binance_atr_are_blocked() -> None:
    cash_candidate = evaluate_v6_risk(
        _request(
            market=V6Market.US_CASH,
            grade=SetupGrade.A_CANDIDATE,
            atr_30s=None,
            leverage=None,
        )
    )
    missing_atr = evaluate_v6_risk(_request(atr_30s=None))

    assert "CASH_A_CANDIDATE_UNSUPPORTED" in cash_candidate.blocker_codes
    assert "BINANCE_ATR30S_REQUIRED" in missing_atr.blocker_codes


def test_quantity_rounds_down_and_zero_quantity_rejects() -> None:
    authority = evaluate_v6_risk(_request(quantity_step=Decimal("0.01")))
    zero = evaluate_v6_risk(
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
        ({"current_open_structural_risk": Decimal("10.01")}, "OPEN_RISK_LIMIT"),
        ({"leverage": 8}, "BINANCE_LEVERAGE_LIMIT"),
    ),
)
def test_loss_open_risk_and_leverage_gates_fail_closed(
    changes: dict[str, object],
    blocker: str,
) -> None:
    authority = evaluate_v6_risk(_request(**changes))

    assert authority.allowed is False
    assert blocker in authority.blocker_codes


def test_worse_current_equity_is_the_risk_base() -> None:
    request = _request(current_equity=Decimal("2200"))
    higher = evaluate_v6_risk(request)
    lower = evaluate_v6_risk(replace(request, current_equity=Decimal("1500")))

    assert higher.risk_base == Decimal("2000")
    assert lower.risk_base == Decimal("1500")
    assert lower.quantity <= higher.quantity


def test_spread_wider_than_three_ticks_is_blocked() -> None:
    authority = evaluate_v6_risk(
        _request(tick_size=Decimal("0.1"), spread=Decimal("0.31"))
    )

    assert "SPREAD_ABOVE_THREE_TICKS" in authority.blocker_codes
    assert authority.quantity == Decimal(0)


def test_spread_of_exactly_three_ticks_is_allowed() -> None:
    authority = evaluate_v6_risk(
        _request(tick_size=Decimal("0.1"), spread=Decimal("0.30"))
    )

    assert "SPREAD_ABOVE_THREE_TICKS" not in authority.blocker_codes


def test_size_multiplier_scales_the_quantity() -> None:
    full = evaluate_v6_risk(_request())
    halved = evaluate_v6_risk(_request(size_multiplier=Decimal("0.5")))

    # The halved size is floored to the quantity step, never rounded up.
    assert halved.quantity == Decimal("1.551")
    assert full.quantity == Decimal("3.103")
    assert halved.quantity <= full.quantity / 2
    assert halved.risk_fraction == full.risk_fraction


def test_max_quantity_caps_the_size() -> None:
    authority = evaluate_v6_risk(_request(max_quantity=Decimal("0.5")))

    assert authority.quantity == Decimal("0.5")


def test_size_multiplier_must_be_within_zero_and_one() -> None:
    for invalid in (Decimal(0), Decimal("1.5"), Decimal("-1")):
        with pytest.raises(ValueError, match="size_multiplier"):
            _request(size_multiplier=invalid)


def test_session_trade_count_below_the_bound_is_allowed() -> None:
    """Section 6 refuses a fixed daily count, so seven trades must pass."""
    authority = evaluate_v6_risk(_request(session_trade_count=7))

    assert "SESSION_TRADE_UPPER_BOUND" not in authority.blocker_codes
    assert authority.quantity > Decimal(0)


def test_the_eighth_trade_of_a_session_is_blocked() -> None:
    authority = evaluate_v6_risk(_request(session_trade_count=8))

    assert "SESSION_TRADE_UPPER_BOUND" in authority.blocker_codes
    assert authority.quantity == Decimal(0)


def test_session_trade_count_must_be_a_non_negative_integer() -> None:
    with pytest.raises(ValueError, match="session_trade_count"):
        _request(session_trade_count=-1)


def test_reaching_the_session_objective_stops_new_entries() -> None:
    authority = evaluate_v6_risk(_request(session_objective_reached=True))

    assert "SESSION_OBJECTIVE_REACHED" in authority.blocker_codes
    assert authority.quantity == Decimal(0)


def test_an_unmet_objective_does_not_block() -> None:
    authority = evaluate_v6_risk(_request(session_objective_reached=False))

    assert "SESSION_OBJECTIVE_REACHED" not in authority.blocker_codes
