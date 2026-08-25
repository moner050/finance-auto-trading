from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.strategies.david_v6.metodo import MetodoFacts, evaluate_metodo
from autotrader.strategies.david_v6.models import EvidenceState, V6Market

START = datetime(2026, 1, 1, tzinfo=UTC)


def _bars(
    closes: tuple[Decimal, ...],
    *,
    final_volume: Decimal = Decimal("1"),
) -> tuple[CompletedOhlcvBar, ...]:
    return tuple(
        CompletedOhlcvBar(
            timestamp=START + timedelta(days=index),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=final_volume if index == len(closes) - 1 else Decimal("1"),
        )
        for index, close in enumerate(closes)
    )


def test_metodo_emits_same_bar_sma_and_macd_confirmation_for_cash() -> None:
    closes = (Decimal("100"),) * 200 + (Decimal("99"), Decimal("110"))
    bars = _bars(closes, final_volume=Decimal("7"))

    item = evaluate_metodo(
        market=V6Market.US_CASH,
        daily_bars=bars,
        decision_at=bars[-1].timestamp + timedelta(days=1),
    )

    assert item.state is EvidenceState.AVAILABLE
    assert type(item.value) is MetodoFacts
    assert (
        item.value.trend_up,
        item.value.sma_6_70_cross_up,
        item.value.macd_cross_up_above_zero,
        item.value.same_bar_a_confirmation,
    ) == (True, True, True, True)
    assert item.value.latest_volume == Decimal("7")
    assert item.provenance is not None
    assert item.provenance.source == "DAVID_V6_DERIVED"
    assert item.provenance.observed_at == bars[-1].timestamp


def test_metodo_does_not_turn_volume_into_an_unapproved_confirmation() -> None:
    closes = (Decimal("100"),) * 202
    bars = _bars(closes, final_volume=Decimal("999999"))

    item = evaluate_metodo(
        market=V6Market.KRX_CASH,
        daily_bars=bars,
        decision_at=bars[-1].timestamp + timedelta(days=1),
    )

    assert item.state is EvidenceState.AVAILABLE
    assert type(item.value) is MetodoFacts
    assert item.value.latest_volume == Decimal("999999")
    assert item.value.normal_technical_confirmation is False
    assert item.value.same_bar_a_confirmation is False


def test_metodo_returns_unavailable_for_insufficient_daily_warmup() -> None:
    bars = _bars((Decimal("100"),) * 200)

    item = evaluate_metodo(
        market=V6Market.KRX_CASH,
        daily_bars=bars,
        decision_at=bars[-1].timestamp + timedelta(days=1),
    )

    assert item.state is EvidenceState.UNAVAILABLE
    assert item.value is None
    assert item.provenance is None
    assert item.blocker_code == "METODO_DAILY_WARMUP_UNAVAILABLE"


def test_metodo_is_not_applicable_to_binance() -> None:
    item = evaluate_metodo(
        market=V6Market.BINANCE_USDM,
        daily_bars=(),
        decision_at=START,
    )

    assert item.state is EvidenceState.NOT_APPLICABLE
    assert item.value is None
    assert item.provenance is None
    assert item.blocker_code == "METODO_CASH_ONLY"
