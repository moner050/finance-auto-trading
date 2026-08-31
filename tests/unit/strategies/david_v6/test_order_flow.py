from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from autotrader.domain.enums import Side
from autotrader.strategies.david_v6.models import EvidenceState
from autotrader.strategies.david_v6.order_flow import (
    BIG_TRADE_EXTREME_QUANTILE,
    BIG_TRADE_NORMAL_QUANTILE,
    MAXIMUM_BIG_TRADE_MARKERS,
    MINIMUM_BIG_TRADE_EVENTS,
    AggressorSide,
    BigTradeClass,
    BigTradeCluster,
    BigTradesUnmeasured,
    OrderFlowFacts,
    OrderFlowThresholds,
    TradePrint,
    aggregate_order_flow,
    big_trade_quantile,
    blocking_big_trade_ahead,
)

START = datetime(2026, 8, 24, tzinfo=UTC)


def _thresholds(**changes: object) -> OrderFlowThresholds:
    values: dict[str, object] = {
        "tick_size": Decimal("1"),
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
    # Four prints cannot rank a Big Trade. Section 22.5 sizes an event against
    # the window's own distribution, and a window this thin has none.
    assert facts.big_trades is None


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
        thresholds=_thresholds(),
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
        thresholds=_thresholds(),
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
        thresholds=_thresholds(),
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


def _many(count: int) -> tuple[TradePrint, ...]:
    """Separate events, one per second, so each is its own aggregation.

    Sizes vary, because a real tape does and a flat one is the degenerate
    case the marker cap exists for.
    """
    return tuple(
        _trade(f"e{index}", seconds=index, price="100", quantity=str(1 + index % 7))
        for index in range(count)
    )


def test_a_thin_window_says_it_cannot_tell_rather_than_finding_nothing() -> None:
    """Not finding an obstacle and not having looked are different answers.
    Only the second may not be read as a clear path."""
    facts = aggregate_order_flow(
        _many(MINIMUM_BIG_TRADE_EVENTS - 1),
        window_start=START,
        window_end=START + timedelta(seconds=MINIMUM_BIG_TRADE_EVENTS + 1),
        thresholds=_thresholds(),
    )

    assert facts.big_trades is None
    with pytest.raises(BigTradesUnmeasured):
        blocking_big_trade_ahead(facts, side=Side.BUY, reference_price=Decimal("100"))


def test_the_marker_is_the_top_of_the_windows_own_distribution() -> None:
    """Section 22.5: normal at the 0.995 quantile of aggregated event
    notionals, extreme at 0.999. Two hundred ordinary events and one large one
    leave the large one marked and nothing else."""
    ordinary = _many(MINIMUM_BIG_TRADE_EVENTS)
    whale = _trade(
        "whale", seconds=MINIMUM_BIG_TRADE_EVENTS, price="100", quantity="10000"
    )

    facts = aggregate_order_flow(
        (*ordinary, whale),
        window_start=START,
        window_end=START + timedelta(seconds=MINIMUM_BIG_TRADE_EVENTS + 2),
        thresholds=_thresholds(),
    )

    assert facts.big_trades is not None
    assert facts.big_trades[-1].summed_notional == Decimal("1000000")
    assert facts.big_trades[-1].classification is BigTradeClass.EXTREME
    # A handful, not a hundred: the top half-percent of the window.
    assert len(facts.big_trades) <= MAXIMUM_BIG_TRADE_MARKERS


def test_a_typed_notional_is_no_longer_asked_for() -> None:
    """Section 19.1 rejects picking one: the filter is calibrated by how many
    markers a session yields, which a fixed number stops doing the moment the
    market changes character."""
    fields = set(OrderFlowThresholds.__dataclass_fields__)

    assert "normal_big_trade_notional" not in fields
    assert "extreme_big_trade_notional" not in fields


def test_the_quantile_takes_a_value_an_event_actually_had() -> None:
    """Nearest rank, not interpolated: an interpolated quantile invents a
    notional no event had, and events are compared against it."""
    values = tuple(Decimal(item) for item in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10))

    assert big_trade_quantile(values, Decimal("0.5")) == Decimal(5)
    assert big_trade_quantile(values, Decimal("0.995")) == Decimal(10)
    assert big_trade_quantile(values, Decimal("1")) == Decimal(10)


def test_the_two_quantiles_are_the_documented_ones() -> None:
    assert Decimal("0.995") == BIG_TRADE_NORMAL_QUANTILE
    assert Decimal("0.999") == BIG_TRADE_EXTREME_QUANTILE


def test_a_flat_window_cannot_mark_every_event_an_obstacle() -> None:
    """A quantile compares with `>=`, so identically sized events all clear it.
    Section 22.5 controls that with a per-session event count; without the cap
    a window of two hundred equal prints reports two hundred obstacles."""
    flat = tuple(
        _trade(f"f{index}", seconds=index, price="100", quantity="1")
        for index in range(MINIMUM_BIG_TRADE_EVENTS + 1)
    )

    facts = aggregate_order_flow(
        flat,
        window_start=START,
        window_end=START + timedelta(seconds=MINIMUM_BIG_TRADE_EVENTS + 2),
        thresholds=_thresholds(),
    )

    assert facts.big_trades is not None
    assert len(facts.big_trades) == MAXIMUM_BIG_TRADE_MARKERS
