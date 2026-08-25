from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import OrderStyle, Side
from autotrader.research.david_v6.backtest import (
    BacktestFill,
    CostModel,
    NoFill,
    WalkForwardWindow,
    exact_next_bar_fill,
    run_walk_forward,
)
from autotrader.shared.ids import new_uuid7
from autotrader.strategies.david_v6.models import (
    EvidenceState,
    SetupGrade,
    StrategyFamily,
    V6Decision,
    V6Market,
)

SIGNAL_AT = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)
FIVE_MINUTES = timedelta(minutes=5)


def _bar(
    timestamp: datetime,
    *,
    open_price: str = "100",
    close_price: str = "101",
) -> CompletedOhlcvBar:
    opening = Decimal(open_price)
    closing = Decimal(close_price)
    return CompletedOhlcvBar(
        timestamp=timestamp,
        open=opening,
        high=max(opening, closing),
        low=min(opening, closing),
        close=closing,
        volume=Decimal("10"),
    )


def _decision() -> V6Decision:
    return V6Decision(
        id=new_uuid7(),
        strategy_version_id=new_uuid7(),
        setup_id=new_uuid7(),
        feature_snapshot_id=new_uuid7(),
        instrument_id=new_uuid7(),
        market=V6Market.BINANCE_USDM,
        family=StrategyFamily.HLIT,
        grade=SetupGrade.NORMAL,
        side=Side.BUY,
        order_style=OrderStyle.MARKET,
        matched_indicators=(),
        blockers=(),
        planned_entry=Decimal("100"),
        structural_stop=Decimal("99"),
        target_price=Decimal("103"),
        risk_fraction=Decimal("0.0025"),
        calculated_quantity=Decimal("2"),
        expected_cost=Decimal("0.8"),
        source_evidence_hashes=(b"d" * 32,),
        completed_evidence_at=SIGNAL_AT,
        generated_at=SIGNAL_AT,
        valid_until=SIGNAL_AT + timedelta(minutes=15),
    )


def _window(**changes: datetime) -> WalkForwardWindow:
    values = {
        "train_started_at": SIGNAL_AT - timedelta(days=10),
        "train_ended_at": SIGNAL_AT - timedelta(days=1),
        "test_started_at": SIGNAL_AT - timedelta(hours=1),
        "test_ended_at": SIGNAL_AT + timedelta(hours=1),
    }
    values.update(changes)
    return WalkForwardWindow(**values)


def _cost_model(value: Decimal | None = Decimal("0.4")) -> CostModel:
    return CostModel(
        timeframe=FIVE_MINUTES,
        round_trip_cost_per_unit=value,
        cost_source_digest=b"c" * 32 if value is not None else None,
        bar_source_digest=b"b" * 32,
    )


def test_exact_next_bar_fill_never_substitutes_a_later_bar() -> None:
    later = _bar(SIGNAL_AT + timedelta(minutes=10))

    missing = exact_next_bar_fill(
        signal_at=SIGNAL_AT,
        timeframe=FIVE_MINUTES,
        bars=(later,),
    )
    filled = exact_next_bar_fill(
        signal_at=SIGNAL_AT,
        timeframe=FIVE_MINUTES,
        bars=(_bar(SIGNAL_AT + FIVE_MINUTES), later),
    )

    assert isinstance(missing, NoFill)
    assert missing.reason_code == "MISSING_EXACT_NEXT_BAR"
    assert isinstance(filled, BacktestFill)
    assert filled.filled_at == SIGNAL_AT + FIVE_MINUTES
    assert filled.price == Decimal("100")


def test_walk_forward_rejects_overlapping_train_and_test_windows() -> None:
    with pytest.raises(ValueError, match="overlap"):
        _window(
            train_ended_at=SIGNAL_AT + timedelta(minutes=1),
            test_started_at=SIGNAL_AT,
        )


def test_walk_forward_always_charges_round_trip_cost() -> None:
    evidence = run_walk_forward(
        decisions=(_decision(),),
        bars=(_bar(SIGNAL_AT + FIVE_MINUTES),),
        windows=(_window(),),
        cost_model=_cost_model(),
    )

    assert evidence.state is EvidenceState.AVAILABLE
    assert evidence.sample_count == 1
    assert evidence.gross_pnl == Decimal("2")
    assert evidence.total_cost == Decimal("0.8")
    assert evidence.net_pnl == Decimal("1.2")
    assert len(evidence.windows[0].source_digest) == 32
    assert len(evidence.artifact_digest) == 32


def test_unsupported_cost_data_is_na_instead_of_zero_performance() -> None:
    evidence = run_walk_forward(
        decisions=(_decision(),),
        bars=(_bar(SIGNAL_AT + FIVE_MINUTES),),
        windows=(_window(),),
        cost_model=_cost_model(None),
    )

    assert evidence.state is EvidenceState.NOT_APPLICABLE
    assert evidence.sample_count == 0
    assert evidence.net_pnl is None
    assert evidence.unsupported_reason_codes == ("UNSUPPORTED_COST_MODEL",)


def test_missing_immediate_bar_returns_unknown_walk_forward_evidence() -> None:
    evidence = run_walk_forward(
        decisions=(_decision(),),
        bars=(_bar(SIGNAL_AT + timedelta(minutes=10)),),
        windows=(_window(),),
        cost_model=_cost_model(),
    )

    assert evidence.state is EvidenceState.UNKNOWN
    assert evidence.sample_count == 0
    assert evidence.net_pnl is None
    assert evidence.unsupported_reason_codes == ("MISSING_EXACT_NEXT_BAR",)
