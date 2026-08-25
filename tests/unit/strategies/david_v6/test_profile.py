from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from autotrader.strategies.david_v6.models import EvidenceState
from autotrader.strategies.david_v6.order_flow import TradePrint
from autotrader.strategies.david_v6.profile import build_profile

START = datetime(2026, 8, 24, tzinfo=UTC)


def _trade(
    identifier: str,
    price: str,
    quantity: str,
    second: int,
    *,
    buyer_maker: bool = False,
) -> TradePrint:
    return TradePrint(
        provider_trade_id=identifier,
        occurred_at=START + timedelta(seconds=second),
        price=Decimal(price),
        quantity=Decimal(quantity),
        buyer_maker=buyer_maker,
    )


def test_profile_builds_poc_and_expands_value_area_to_seventy_percent() -> None:
    facts = build_profile(
        (
            _trade("a", "100", "1", 0),
            _trade("b1", "101", "1.5", 1),
            _trade("b2", "101", "0.5", 2, buyer_maker=True),
            _trade("c", "102", "1", 2),
        ),
        tick_size=Decimal("1"),
    )

    assert facts.state is EvidenceState.AVAILABLE
    assert facts.point_of_control == Decimal("101")
    assert facts.value_area_low == Decimal("101")
    assert facts.value_area_high == Decimal("102")
    assert facts.total_notional == Decimal("404")
    assert tuple(level.price for level in facts.levels) == (
        Decimal("100"),
        Decimal("101"),
        Decimal("102"),
    )
    poc_level = facts.levels[1]
    assert poc_level.buy_notional == Decimal("151.5")
    assert poc_level.sell_notional == Decimal("50.5")
    assert poc_level.delta_notional == Decimal("101.0")
    assert poc_level.imbalance_ratio == Decimal("3")


def test_profile_returns_unavailable_for_empty_or_incomplete_price_size() -> None:
    empty = build_profile((), tick_size=Decimal("1"))
    incomplete = build_profile(
        (
            TradePrint(
                provider_trade_id="missing-price",
                occurred_at=START,
                price=None,
                quantity=Decimal("1"),
                buyer_maker=False,
            ),
        ),
        tick_size=Decimal("1"),
    )

    assert empty.state is EvidenceState.UNAVAILABLE
    assert incomplete.state is EvidenceState.UNAVAILABLE
    assert empty.levels == incomplete.levels == ()
