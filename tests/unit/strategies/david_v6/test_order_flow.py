from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from autotrader.domain.enums import Side
from autotrader.strategies.david_v6.models import EvidenceState
from autotrader.strategies.david_v6.order_flow import (
    AggressorSide,
    BigTradeClass,
    BigTradeCluster,
    OrderFlowFacts,
    OrderFlowThresholds,
    TradePrint,
    aggregate_order_flow,
    blocking_big_trade_ahead,
)

START = datetime(2026, 8, 24, tzinfo=UTC)


def _thresholds(**changes: object) -> OrderFlowThresholds:
    values: dict[str, object] = {
        "tick_size": Decimal("1"),
        "normal_big_trade_notional": Decimal("100"),
        "extreme_big_trade_notional": Decimal("200"),
        "delta_p90_notional": Decimal("100"),
        "atr_30s": Decimal("10"),
        "ceros_near_zero_notional": Decimal("10"),
        "ceros_large_notional": Decimal("40"),
    }
    values.update(changes)
    return OrderFlowThresholds(**values)  # type: ignore[arg-type]


def _trade(
    identifier: str,
    *,
    milliseconds: int = 0,
    seconds: int = 0,
    price: str = "100",
    quantity: str = "1",
    buyer_maker: bool | None = False,
) -> TradePrint:
    return TradePrint(
        provider_trade_id=identifier,
        occurred_at=START + timedelta(seconds=seconds, milliseconds=milliseconds),
        price=Decimal(price),
        quantity=Decimal(quantity),
        buyer_maker=buyer_maker,
    )


def test_big_trades_dedupe_by_provider_id_and_retain_unknown_aggressor() -> None:
    first = _trade("a", price="100", quantity="0.5")
    trades = (
        first,
        _trade("b", milliseconds=100, price="101", quantity="0.5"),
        _trade("c", milliseconds=200, price="102", quantity="0.5"),
        first,
        _trade("unknown", milliseconds=300, buyer_maker=None),
        _trade("outside", seconds=60),
    )

    facts = aggregate_order_flow(
        trades,
        window_start=START,
        window_end=START + timedelta(seconds=30),
        thresholds=_thresholds(),
    )

    assert facts.state is EvidenceState.UNKNOWN
    assert facts.trade_count == 4
    assert facts.unknown_aggressor_count == 1
    assert facts.buy_notional == Decimal("151.5")
    assert facts.sell_notional == 0
    assert len(facts.big_trades) == 1
    assert facts.big_trades[0].side is AggressorSide.BUY
    assert facts.big_trades[0].classification is BigTradeClass.NORMAL


def test_reversal_mig_and_secado_use_completed_30_second_bars() -> None:
    trades = (
        _trade("e1", seconds=0, price="100", quantity="10", buyer_maker=True),
        _trade("e2", seconds=5, price="90", quantity="10", buyer_maker=True),
        _trade("e3", seconds=20, price="99", quantity="10", buyer_maker=True),
        _trade("s1", seconds=31, price="98", quantity="3", buyer_maker=True),
        _trade("s2", seconds=50, price="101", quantity="3", buyer_maker=True),
        _trade("r1", seconds=61, price="101", quantity="1", buyer_maker=False),
    )

    facts = aggregate_order_flow(
        trades,
        window_start=START,
        window_end=START + timedelta(seconds=90),
        thresholds=_thresholds(
            normal_big_trade_notional=Decimal("10000"),
            extreme_big_trade_notional=Decimal("20000"),
        ),
    )

    assert facts.reversal_mig is True
    assert facts.secado is True
    assert facts.telemetry_only is True


def test_ceros_requires_two_adjacent_outer_levels_with_extreme_imbalance() -> None:
    trades = (
        _trade("b90", price="90", quantity="1", buyer_maker=False),
        _trade("s90", milliseconds=1, price="90", quantity="0.05", buyer_maker=True),
        _trade("b91", milliseconds=2, price="91", quantity="1", buyer_maker=False),
        _trade("s91", milliseconds=3, price="91", quantity="0.05", buyer_maker=True),
        _trade("top", milliseconds=4, price="100", quantity="1", buyer_maker=False),
    )

    facts = aggregate_order_flow(
        trades,
        window_start=START,
        window_end=START + timedelta(seconds=30),
        thresholds=_thresholds(
            normal_big_trade_notional=Decimal("10000"),
            extreme_big_trade_notional=Decimal("20000"),
        ),
    )

    assert facts.ceros is True
    assert facts.telemetry_only is True


def test_forming_30_second_bar_cannot_confirm_reversal_mig() -> None:
    trades = (
        _trade("e1", seconds=0, price="100", quantity="10", buyer_maker=True),
        _trade("e2", seconds=5, price="90", quantity="10", buyer_maker=True),
        _trade("e3", seconds=20, price="99", quantity="10", buyer_maker=True),
        _trade("forming", seconds=31, price="101", buyer_maker=False),
    )

    facts = aggregate_order_flow(
        trades,
        window_start=START,
        window_end=START + timedelta(seconds=45),
        thresholds=_thresholds(
            normal_big_trade_notional=Decimal("10000"),
            extreme_big_trade_notional=Decimal("20000"),
        ),
    )

    assert facts.reversal_mig is None


def _facts_with(clusters: tuple[BigTradeCluster, ...]) -> OrderFlowFacts:
    return OrderFlowFacts(
        state=EvidenceState.AVAILABLE,
        trade_count=1,
        unknown_aggressor_count=0,
        buy_notional=Decimal("100"),
        sell_notional=Decimal("100"),
        delta_notional=Decimal("0"),
        big_trades=clusters,
        reversal_mig=False,
        continuation_mig=False,
        secado=False,
        ceros=False,
        telemetry_only=False,
    )


def _cluster(side: AggressorSide, low: str, high: str) -> BigTradeCluster:
    return BigTradeCluster(
        side=side,
        started_at=START,
        ended_at=START + timedelta(milliseconds=100),
        low_price=Decimal(low),
        high_price=Decimal(high),
        trade_count=3,
        summed_notional=Decimal("500"),
        classification=BigTradeClass.EXTREME,
    )


def test_selling_big_trade_above_a_long_entry_blocks() -> None:
    facts = _facts_with((_cluster(AggressorSide.SELL, "101", "103"),))

    assert blocking_big_trade_ahead(
        facts, side=Side.BUY, reference_price=Decimal("100")
    )


def test_selling_big_trade_below_a_long_entry_does_not_block() -> None:
    facts = _facts_with((_cluster(AggressorSide.SELL, "95", "97"),))

    assert not blocking_big_trade_ahead(
        facts, side=Side.BUY, reference_price=Decimal("100")
    )


def test_supporting_big_trade_ahead_of_a_long_does_not_block() -> None:
    facts = _facts_with((_cluster(AggressorSide.BUY, "101", "103"),))

    assert not blocking_big_trade_ahead(
        facts, side=Side.BUY, reference_price=Decimal("100")
    )


def test_buying_big_trade_below_a_short_entry_blocks() -> None:
    facts = _facts_with((_cluster(AggressorSide.BUY, "97", "99"),))

    assert blocking_big_trade_ahead(
        facts, side=Side.SELL, reference_price=Decimal("100")
    )


def test_no_big_trades_never_blocks() -> None:
    assert not blocking_big_trade_ahead(
        _facts_with(()), side=Side.BUY, reference_price=Decimal("100")
    )
