from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.strategies.david_v6.zones import ZoneConfig, build_hlit_zones

START = datetime(2026, 8, 20, tzinfo=UTC)


def _bar(day: int, minute: int, price: int) -> CompletedOhlcvBar:
    value = Decimal(price)
    return CompletedOhlcvBar(
        timestamp=START + timedelta(days=day, minutes=minute),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("1"),
    )


def test_zone_touches_count_distinct_completed_bars_not_ohlc_ticks() -> None:
    prices = ((100, 90, 110), (100, 90, 110), (100, 90, 110))
    bars = tuple(
        _bar(day, offset * 5, price)
        for day, daily_prices in enumerate(prices)
        for offset, price in enumerate(daily_prices)
    )

    facts = build_hlit_zones(
        bars,
        ZoneConfig(source_timezone="UTC"),
    )

    hundred = next(
        zone
        for zone in facts.zones
        if zone.lower_boundary <= Decimal("100") <= zone.upper_boundary
    )
    assert hundred.touch_count == 3
    assert hundred.strength == 3
    assert len(hundred.touched_at) == 3


def test_zone_strength_caps_at_five_distinct_bar_touches() -> None:
    bars = tuple(
        _bar(day, offset * 5, 100 if offset < 2 else 90 + 20 * (offset % 2))
        for day in range(3)
        for offset in range(4)
    )

    facts = build_hlit_zones(
        bars,
        ZoneConfig(source_timezone="UTC"),
    )

    assert any(zone.touch_count >= 6 and zone.strength == 5 for zone in facts.zones)
