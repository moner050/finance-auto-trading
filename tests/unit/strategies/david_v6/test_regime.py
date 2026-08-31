from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from autotrader.strategies.david_v6.models import EvidenceState
from autotrader.strategies.david_v6.regime import (
    PessimismInputs,
    RegimeFacts,
    RegimeLabel,
    daily_returns,
    evaluate_regime,
)

COMPLETED_DATE = date(2026, 8, 21)


def _pessimism(**changes: object) -> PessimismInputs:
    values: dict[str, object] = {
        "completed_date": COMPLETED_DATE,
        "volatility_percentile": Decimal("0.90"),
        "put_call_percentile": Decimal("0.90"),
        "breadth_percentile": Decimal("0.50"),
    }
    values.update(changes)
    return PessimismInputs(**values)  # type: ignore[arg-type]


def _evaluate(
    *,
    returns: tuple[Decimal, ...] | None = None,
    atr_ratio: Decimal | None = Decimal("0.50"),
    range_efficiency: Decimal | None = Decimal("0.50"),
    pessimism: PessimismInputs | None = None,
) -> RegimeFacts:
    return evaluate_regime(
        benchmark_returns=(Decimal("0.01"),) * 200 if returns is None else returns,
        atr_ratio=atr_ratio,
        range_efficiency=range_efficiency,
        pessimism_inputs=_pessimism() if pessimism is None else pessimism,
    )


def test_trend_labels_use_completed_return_history() -> None:
    assert _evaluate().trend is RegimeLabel.TREND_UP
    assert _evaluate(returns=(Decimal("-0.001"),) * 200).trend is RegimeLabel.TREND_DOWN
    assert _evaluate(returns=(Decimal("0"),) * 200).trend is RegimeLabel.BALANCE


def test_sideways_and_low_volatility_are_observed_but_gate_nothing() -> None:
    """Section 2.1's regime is the SMA rule alone. These two were added on top
    of it and used to exclude a trade by themselves, which refused in
    conditions the author traded through."""
    sideways = _evaluate(range_efficiency=Decimal("0.20"))
    low_volatility = _evaluate(atr_ratio=Decimal("0.20"))

    assert (sideways.sideways, sideways.low_volatility) == (True, False)
    assert (low_volatility.sideways, low_volatility.low_volatility) == (False, True)
    # Reported, and not a reason to stand aside.
    assert sideways.excluded is False
    assert low_volatility.excluded is False


def test_the_regime_is_excluded_only_when_its_own_rule_cannot_be_read() -> None:
    assert _evaluate(returns=(Decimal("0.01"),) * 199).excluded is True
    assert _evaluate().excluded is False


def test_the_two_observations_are_optional() -> None:
    """They are not in the author's rule, so their absence is not a gap in
    it."""
    facts = _evaluate(atr_ratio=None, range_efficiency=None)

    assert facts.state is EvidenceState.AVAILABLE
    assert facts.sideways is None
    assert facts.low_volatility is None


def test_pessimism_extreme_requires_two_of_three_same_date_quantiles() -> None:
    volatility_and_put_call = _evaluate()
    volatility_and_breadth = _evaluate(
        pessimism=_pessimism(
            put_call_percentile=Decimal("0.89"),
            breadth_percentile=Decimal("0.10"),
        )
    )
    only_one = _evaluate(
        pessimism=_pessimism(
            put_call_percentile=Decimal("0.89"),
            breadth_percentile=Decimal("0.11"),
        )
    )

    assert volatility_and_put_call.pessimism_extreme is True
    assert volatility_and_breadth.pessimism_extreme is True
    assert only_one.pessimism_extreme is False


def test_a_missing_pessimism_component_leaves_the_regime_available() -> None:
    """Pessimism belongs to one signal — a MACD cross below zero — not to the
    regime. Blocking every decision on it made an input the author used
    occasionally into a precondition for trading at all."""
    facts = _evaluate(pessimism=_pessimism(put_call_percentile=None))

    assert facts.state is EvidenceState.AVAILABLE


def test_two_measured_components_are_judged_without_the_third() -> None:
    """Section 2.3 marks the quantitative triple as curriculum and the
    detector the author actually used as a newspaper. Waiting for the one
    percentile with no history to rank against was a stricter condition than
    he ever applied."""
    both_extreme = _evaluate(
        pessimism=_pessimism(
            put_call_percentile=None,
            volatility_percentile=Decimal("0.95"),
            breadth_percentile=Decimal("0.05"),
        )
    )
    one_extreme = _evaluate(
        pessimism=_pessimism(
            put_call_percentile=None,
            volatility_percentile=Decimal("0.95"),
            breadth_percentile=Decimal("0.50"),
        )
    )

    assert both_extreme.pessimism_extreme is True
    assert one_extreme.pessimism_extreme is False


def test_the_threshold_does_not_soften_when_a_component_is_missing() -> None:
    """Two of three is a majority; two of two is unanimity. Dropping to "one
    of the two present" would make an extreme easier to call every time a
    measurement went missing, which is backwards."""
    facts = _evaluate(
        pessimism=_pessimism(
            put_call_percentile=None,
            volatility_percentile=Decimal("0.99"),
            breadth_percentile=Decimal("0.50"),
        )
    )

    assert facts.pessimism_extreme is False


def test_one_measured_component_cannot_call_an_extreme() -> None:
    """Not "no extreme" — nothing to judge. One agreeing component was never
    two."""
    facts = _evaluate(
        pessimism=_pessimism(
            put_call_percentile=None,
            breadth_percentile=None,
            volatility_percentile=Decimal("0.99"),
        )
    )

    assert facts.pessimism_extreme is None


def test_all_three_present_is_unchanged() -> None:
    """The rule that ran until the third component had a history still runs
    once it has one."""
    facts = _evaluate(
        pessimism=_pessimism(
            volatility_percentile=Decimal("0.95"),
            put_call_percentile=Decimal("0.95"),
            breadth_percentile=Decimal("0.50"),
        )
    )

    assert facts.pessimism_extreme is True


def test_a_day_the_market_has_not_finished_is_not_judged() -> None:
    facts = _evaluate(pessimism=_pessimism(completed_date=None))

    assert facts.pessimism_extreme is None


def test_pessimism_may_be_absent_entirely() -> None:
    facts = evaluate_regime(benchmark_returns=(Decimal("0.01"),) * 200)

    assert facts.state is EvidenceState.AVAILABLE
    assert facts.pessimism_extreme is None


def test_fewer_than_200_completed_returns_are_unavailable() -> None:
    facts = _evaluate(returns=(Decimal("0.01"),) * 199)

    assert facts.state is EvidenceState.UNAVAILABLE
    assert facts.trend is None


def test_daily_returns_reproduce_the_authors_rule_from_closes() -> None:
    """Section 2.1 gives the regime as SMA 6/70/200 on the instrument itself.
    Rebasing is a positive scale and a moving average is linear, so the same
    answer comes out whether the trend is taken over closes or over returns
    rebuilt from them."""
    closes = [Decimal(100) + Decimal(index) for index in range(260)]

    returns = daily_returns(closes)

    assert len(returns) == len(closes) - 1
    facts = evaluate_regime(
        benchmark_returns=returns,
        atr_ratio=Decimal("0.5"),
        range_efficiency=Decimal("0.5"),
        pessimism_inputs=PessimismInputs(
            completed_date=date(2026, 8, 27),
            volatility_percentile=Decimal("0.5"),
            put_call_percentile=Decimal("0.5"),
            breadth_percentile=Decimal("0.5"),
        ),
    )
    # A monotonically rising series is the author's uptrend: the 200 slopes up,
    # the 70 is above it, and the 70 slopes up.
    assert facts.trend is RegimeLabel.TREND_UP


def test_a_non_positive_close_is_refused() -> None:
    with pytest.raises(ValueError, match="close must be positive"):
        daily_returns([Decimal(100), Decimal(0)])
