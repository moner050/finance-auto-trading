"""The authority a Binance USD-M order is written under.

The adapter refuses to send anything without one and refuses one older than
thirty seconds, so the two things worth testing without a database are how the
cache ages and what it does when it has never been read.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from autotrader.apps.trader.live_authority import (
    ConfigurationCache,
    VenueConfiguration,
)
from autotrader.integrations.brokers.binance_usdm.configuration import (
    ConfigurationReport,
)
from autotrader.integrations.brokers.binance_usdm.orders import (
    BinanceUsdmSymbolFilters,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _report(**changes: object) -> ConfigurationReport:
    values: dict[str, object] = {
        "ready": True,
        "blockers": (),
        "position_mode": "ONE_WAY",
        "margin_type": "ISOLATED",
        "auto_add_margin": False,
        "leverage": 3,
        "can_trade": True,
        "multi_assets_margin": False,
        "account_transfer_out_enabled": False,
    }
    values.update(changes)
    return ConfigurationReport(**values)  # pyright: ignore[reportArgumentType]


def _configuration(read_at: datetime) -> VenueConfiguration:
    return VenueConfiguration(
        report=_report(),
        filters=BinanceUsdmSymbolFilters(
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            minimum_quantity=Decimal("0.001"),
            minimum_notional=Decimal("100"),
            captured_at=read_at,
        ),
        read_at=read_at,
    )


@dataclass
class _Source:
    at: list[datetime]
    reads: int = 0

    async def read(self) -> VenueConfiguration:
        self.reads += 1
        return _configuration(self.at.pop(0))


@pytest.mark.asyncio
async def test_nothing_read_yet_is_nothing_known() -> None:
    """And an order sent on nothing known is an order sent on nothing."""
    cache = ConfigurationCache(
        source=_Source([NOW]),  # pyright: ignore[reportArgumentType]
        every=timedelta(seconds=10),
    )
    assert cache.current is None


@pytest.mark.asyncio
async def test_a_reading_inside_the_interval_is_not_taken_again() -> None:
    """Three round trips per order would put a third of a minute of network
    between deciding and sending."""
    source = _Source([NOW])
    cache = ConfigurationCache(
        source=source,  # pyright: ignore[reportArgumentType]
        every=timedelta(seconds=10),
    )
    first = await cache.refresh(NOW)
    again = await cache.refresh(NOW + timedelta(seconds=9))

    assert source.reads == 1
    assert first is again


@pytest.mark.asyncio
async def test_a_reading_past_the_interval_is_taken_again() -> None:
    later = NOW + timedelta(seconds=10)
    source = _Source([NOW, later])
    cache = ConfigurationCache(
        source=source,  # pyright: ignore[reportArgumentType]
        every=timedelta(seconds=10),
    )
    await cache.refresh(NOW)
    fresh = await cache.refresh(later)

    assert source.reads == 2
    assert fresh is not None
    assert fresh.read_at == later


@pytest.mark.asyncio
async def test_the_last_reading_is_returned_whatever_its_age() -> None:
    """Deciding what is too old belongs to the adapter, which has the window.
    A cache that withheld a stale reading would turn one refusal into two with
    different messages."""
    source = _Source([NOW, NOW + timedelta(hours=1)])
    cache = ConfigurationCache(
        source=source,  # pyright: ignore[reportArgumentType]
        every=timedelta(seconds=10),
    )
    await cache.refresh(NOW)
    held = cache.current

    assert held is not None
    assert held.read_at == NOW


def test_a_refresh_interval_has_to_be_one() -> None:
    for every in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError):
            ConfigurationCache(
                source=_Source([NOW]),  # pyright: ignore[reportArgumentType]
                every=every,
            )


def test_a_configuration_that_is_not_ready_carries_its_reasons() -> None:
    """What the authority reports instead of sending: the venue's own list."""
    report = _report(ready=False, blockers=("MARGIN_TYPE_NOT_ISOLATED",))
    assert report.ready is False
    assert report.blockers == ("MARGIN_TYPE_NOT_ISOLATED",)
    assert replace(report, ready=True).ready is True
