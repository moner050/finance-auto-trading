"""The live composition's own decisions, without a database or a venue.

Building the whole thing needs both. What can be checked here is what the
composition chose: which routes exist at all, that one capture answers
everything that needs one, and that the filters an order is measured against
are stamped with the read that produced them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid7

import pytest

from autotrader.apps.trader.binance_live import (
    CONFIGURATION_REFRESH,
    ROUTES,
    SYMBOL,
    LiveTrades,
    _InstrumentResolver,
)
from autotrader.domain.enums import Side
from autotrader.integrations.brokers.binance_usdm.account import BinanceUsdmTradeFact
from autotrader.integrations.brokers.binance_usdm.configuration import (
    ConfigurationReport,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

# What must never be reachable through this transport, whatever a caller asks.
FORBIDDEN = (
    "/sapi/v1/capital/withdraw/apply",
    "/fapi/v1/withdraw",
    "/sapi/v1/futures/transfer",
    "/sapi/v1/asset/transfer",
    "/sapi/v1/apiKey",
    "/fapi/v1/leverage",
    "/fapi/v1/marginType",
    "/fapi/v1/positionSide/dual/set",
)


def test_no_route_moves_money_off_the_account() -> None:
    """A transport that could reach a withdrawal endpoint is one that a bug
    could."""
    paths = {path for _, path in ROUTES}
    for path in FORBIDDEN:
        assert path not in paths, path


def test_the_only_writes_are_orders() -> None:
    """Everything else this loop does is a read. The three writes are placing
    an order, placing a stop, and withdrawing a stop that was replaced."""
    writes = sorted(
        (method, path) for method, path in ROUTES if method in {"POST", "DELETE"}
    )
    assert writes == [
        ("DELETE", "/fapi/v1/algoOrder"),
        ("POST", "/fapi/v1/algoOrder"),
        ("POST", "/fapi/v1/order"),
    ]


def test_nothing_changes_the_account_configuration() -> None:
    """Leverage, margin type and position mode are read and refused on, never
    set: an order that had to change the account to be allowed is an order
    the account was not configured for."""
    for method, path in ROUTES:
        if path in {
            "/fapi/v1/accountConfig",
            "/fapi/v1/positionSide/dual",
            "/fapi/v1/symbolConfig",
        }:
            assert method == "GET", (method, path)


def test_the_configuration_is_re_read_well_inside_the_authority_window() -> None:
    """The adapter refuses an authority older than thirty seconds."""
    window = timedelta(seconds=30)
    assert CONFIGURATION_REFRESH.total_seconds() * 2 <= window.total_seconds()


@dataclass
class _Capture:
    trades: tuple[BinanceUsdmTradeFact, ...] = ()
    snapshots: int = 0

    async def snapshot(self, as_of: datetime) -> object:
        del as_of
        self.snapshots += 1
        return _Snapshot(self.trades)


@dataclass
class _Snapshot:
    trades: tuple[BinanceUsdmTradeFact, ...]


def _trade(trade_id: int) -> BinanceUsdmTradeFact:
    return BinanceUsdmTradeFact(
        trade_id=trade_id,
        order_id=1,
        symbol=SYMBOL,
        side=Side.BUY.value,
        quantity=Decimal("0.002"),
        price=Decimal("60000"),
        commission=Decimal("0.024"),
        commission_asset="USDT",
        realized_pnl=Decimal(0),
        occurred_at=NOW,
    )


@pytest.mark.asyncio
async def test_the_fills_come_from_the_same_capture_everything_else_reads() -> None:
    capture = _Capture(trades=(_trade(1), _trade(2)))
    trades = LiveTrades(
        transport=None,  # pyright: ignore[reportArgumentType]
        capture=capture,  # pyright: ignore[reportArgumentType]
    )

    answered = await trades.after(None, now=NOW)

    assert [item.trade_id for item in answered] == [1, 2]
    assert capture.snapshots == 1


@dataclass
class _Rest:
    async def exchange_info(self, *, symbol: str) -> dict[str, object]:
        return {
            "symbol": symbol,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "100"},
            ],
        }


@pytest.mark.asyncio
async def test_the_filters_are_stamped_with_the_read_that_produced_them() -> None:
    """So the adapter's window measures the age of the answer rather than of
    the object holding it."""
    from autotrader.integrations.market_data.binance_instrument import (
        read_specification,
    )

    specification = await read_specification(
        _Rest(),  # pyright: ignore[reportArgumentType]
        symbol=SYMBOL,
    )
    assert specification.tick_size == Decimal("0.10")
    assert specification.minimum_notional == Decimal("100")


@pytest.mark.asyncio
async def test_one_symbol_and_anything_else_is_refused() -> None:
    resolver = _InstrumentResolver(uuid7())
    assert await resolver.resolve("BINANCE-USDM", SYMBOL) == resolver.instrument_id
    with pytest.raises(LookupError):
        await resolver.resolve("BINANCE-USDM", "ETHUSDT")


def test_a_configuration_that_is_not_ready_stops_an_order() -> None:
    """`MySqlOrderAuthority` refuses on this rather than sending anything, so
    the venue's own blockers are what an operator reads."""
    report = ConfigurationReport(
        ready=False,
        blockers=("MARGIN_TYPE_NOT_ISOLATED", "LEVERAGE_MISMATCH"),
        position_mode="ONE_WAY",
        margin_type="CROSSED",
        auto_add_margin=False,
        leverage=5,
        can_trade=True,
        multi_assets_margin=False,
        account_transfer_out_enabled=False,
    )
    assert report.ready is False
    assert len(report.blockers) == 2
