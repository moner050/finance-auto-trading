from __future__ import annotations

from datetime import date
from decimal import Decimal

from autotrader.strategies.david_v6.models import EvidenceState
from autotrader.strategies.david_v6.regime import (
    PessimismInputs,
    RegimeFacts,
    RegimeLabel,
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
    atr_ratio: Decimal = Decimal("0.50"),
    range_efficiency: Decimal = Decimal("0.50"),
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


def test_sideways_and_low_volatility_bottom_quintiles_are_independent() -> None:
    sideways = _evaluate(range_efficiency=Decimal("0.20"))
    low_volatility = _evaluate(atr_ratio=Decimal("0.20"))

    assert (sideways.sideways, sideways.low_volatility, sideways.excluded) == (
        True,
        False,
        True,
    )
    assert (
        low_volatility.sideways,
        low_volatility.low_volatility,
        low_volatility.excluded,
    ) == (False, True, True)


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


def test_unavailable_pessimism_component_makes_composite_unavailable() -> None:
    facts = _evaluate(
        pessimism=_pessimism(put_call_percentile=None),
    )

    assert facts.state is EvidenceState.UNAVAILABLE
    assert facts.pessimism_extreme is None


def test_fewer_than_200_completed_returns_are_unavailable() -> None:
    facts = _evaluate(returns=(Decimal("0.01"),) * 199)

    assert facts.state is EvidenceState.UNAVAILABLE
    assert facts.trend is None
