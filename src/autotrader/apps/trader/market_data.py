"""Feed the loop from Binance USD-M market data.

Two ports the loop declares and nothing implemented: the source of the next
evaluation, and the bar that settles a paper order. Both read completed bars
only, and both refuse to answer rather than guess when the data is not there.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from autotrader.apps.trader.tick import TickContext
from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import Side
from autotrader.integrations.brokers.internal_paper import (
    PaperExecutionBar,
    PaperOrderCommand,
)
from autotrader.risk.v6 import V6RiskContext
from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.assembly import AssemblyInputs, AssemblySource
from autotrader.strategies.david_v6.calendar import EventCalendar
from autotrader.strategies.david_v6.costs import FeeSchedule
from autotrader.strategies.david_v6.manifest import V6Manifest
from autotrader.strategies.david_v6.models import V6Market
from autotrader.strategies.david_v6.order_flow import OrderFlowThresholds, TradePrint
from autotrader.strategies.david_v6.regime import PessimismInputs
from autotrader.strategies.david_v6.sessions import ExchangeCalendar

HLIT_TIMEFRAME = timedelta(minutes=5)
DAILY_TIMEFRAME = timedelta(days=1)


class CompletedBars(Protocol):
    async def completed_bars(
        self,
        timeframe: timedelta,
        end_at: datetime,
        *,
        history: timedelta | None = None,
    ) -> tuple[CompletedOhlcvBar, ...]: ...

    async def trade_prints(
        self, start_at: datetime, end_at: datetime
    ) -> tuple[TradePrint, ...]: ...


class RiskContextFactory(Protocol):
    def build(
        self, *, bars: tuple[CompletedOhlcvBar, ...], now: datetime
    ) -> V6RiskContext | None:
        """The account-side context, or None when it cannot be priced."""
        ...


@dataclass(frozen=True, slots=True)
class BinanceLoopInputs:
    """What the loop needs besides the bars, held for the account it trades."""

    instrument_id: UUID
    manifest: V6Manifest
    calendar: ExchangeCalendar
    order_flow_thresholds: OrderFlowThresholds
    fee_schedule: FeeSchedule
    tick_size: Decimal
    spread: Decimal
    stop_slippage_q95: Decimal
    quantity: Decimal
    pessimism: PessimismInputs
    benchmark_returns: tuple[Decimal, ...]
    # Optional, because `evaluate_regime` treats them as optional. Section
    # 2.1 gives the regime as SMA 6/70/200 alone and consults neither, so
    # demanding them here made the loop unstartable over two observations
    # nothing reads.
    atr_ratio: Decimal | None = None
    range_efficiency: Decimal | None = None
    events: EventCalendar | None = None
    trade_window: timedelta = field(default=timedelta(minutes=30))


class BinanceContextSource:
    """The next evaluation, once a five minute bar the loop has not seen closes."""

    def __init__(
        self,
        *,
        market_data: CompletedBars,
        inputs: BinanceLoopInputs,
        risk: RiskContextFactory,
    ) -> None:
        self._market_data = market_data
        self._inputs = inputs
        self._risk = risk
        self._watermark: datetime | None = None

    @property
    def watermark(self) -> datetime | None:
        """The close of the last bar this source has already evaluated."""
        return self._watermark

    async def context_for(self, now: datetime) -> TickContext | None:
        moment = require_utc(now)
        bars = await self._market_data.completed_bars(HLIT_TIMEFRAME, moment)
        if not bars:
            return None
        latest = bars[-1].timestamp
        # One evaluation per bar. Re-running the same bar would record a second
        # decision for evidence that has not changed.
        if self._watermark is not None and latest <= self._watermark:
            return None
        risk_context = self._risk.build(bars=bars, now=moment)
        if risk_context is None:
            return None
        daily = await self._market_data.completed_bars(DAILY_TIMEFRAME, moment)
        trades = await self._market_data.trade_prints(
            moment - self._inputs.trade_window, moment
        )
        self._watermark = latest
        return TickContext(
            inputs=self._assembly_inputs(bars, daily, trades, moment),
            manifest=self._inputs.manifest,
            risk_context=risk_context,
            now=moment,
        )

    def _assembly_inputs(
        self,
        bars: tuple[CompletedOhlcvBar, ...],
        daily: tuple[CompletedOhlcvBar, ...],
        trades: tuple[TradePrint, ...],
        now: datetime,
    ) -> AssemblyInputs:
        held = self._inputs
        return AssemblyInputs(
            market=V6Market.BINANCE_USDM,
            instrument_id=held.instrument_id,
            decision_at=now,
            source=AssemblySource(
                name="BINANCE_USDM_PUBLIC",
                timezone="UTC",
                captured_at=now,
            ),
            bars={"5m": bars, "1d": daily},
            calendar=held.calendar,
            events=held.events,
            benchmark_returns=held.benchmark_returns,
            atr_ratio=held.atr_ratio,
            range_efficiency=held.range_efficiency,
            pessimism=held.pessimism,
            trades=trades,
            order_flow_thresholds=held.order_flow_thresholds,
            fee_schedule=held.fee_schedule,
            spread=held.spread,
            quantity=held.quantity,
            stop_slippage_q95=held.stop_slippage_q95,
            tick_size=held.tick_size,
        )


class BinanceExecutionBars:
    """The bar that settles a staged paper order, once it has closed."""

    def __init__(self, market_data: CompletedBars) -> None:
        self._market_data = market_data

    async def bar_at(
        self, command: PaperOrderCommand, *, now: datetime
    ) -> PaperExecutionBar | None:
        expected_at = require_utc(command.signal_at) + command.timeframe
        moment = require_utc(now)
        if command.trigger_price is None:
            bars = await self._market_data.completed_bars(
                command.timeframe, expected_at + command.timeframe
            )
            bar = next((row for row in bars if row.timestamp == expected_at), None)
        else:
            # A stop rests until a bar reaches it, so the window runs to the
            # present rather than to one known bar.
            bars = await self._market_data.completed_bars(command.timeframe, moment)
            bar = next(
                (
                    row
                    for row in bars
                    if row.timestamp >= expected_at
                    and _reaches(command.side, command.trigger_price, row)
                ),
                None,
            )
        if bar is None:
            # Not closed yet, or not reached yet. Neither is a missing bar.
            return None
        return PaperExecutionBar(
            bar=bar,
            available_quantity=command.quantity,
            source_digest=_digest_of(bar),
        )

    async def next_bar(
        self, command: PaperOrderCommand, *, now: datetime
    ) -> PaperExecutionBar | None:
        return await self.bar_at(command, now=now)


def _reaches(side: Side, trigger: Decimal, bar: CompletedOhlcvBar) -> bool:
    """Whether this bar traded through the stop.

    A stop that exits a long sits below the price and is reached on the way
    down; one that exits a short sits above it.
    """
    return bar.high >= trigger if side is Side.BUY else bar.low <= trigger


def _digest_of(bar: CompletedOhlcvBar) -> bytes:
    payload = {
        "timestamp": bar.timestamp.isoformat(),
        "open": format(bar.open, "f"),
        "high": format(bar.high, "f"),
        "low": format(bar.low, "f"),
        "close": format(bar.close, "f"),
        "volume": format(bar.volume, "f"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).digest()


__all__ = (
    "DAILY_TIMEFRAME",
    "HLIT_TIMEFRAME",
    "BinanceContextSource",
    "BinanceExecutionBars",
    "BinanceLoopInputs",
    "CompletedBars",
    "RiskContextFactory",
)
