"""One evaluation per closed bar, measured fresh each time.

`BinanceContextSource` does the same job for the paper loop, holding one set
of inputs for the whole run. This one asks `LiveBinanceInputs` to rebuild them
every pass, because almost all of them are measurements now.

The watermark is the part worth keeping either way: one evaluation per bar.
Re-running a bar records a second decision for evidence that has not changed,
and the promotion evidence counts decisions.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from autotrader.apps.trader.market_data import (
    DAILY_TIMEFRAME,
    HLIT_TIMEFRAME,
    AssemblyInputs,
    AssemblySource,
    CompletedBars,
)
from autotrader.apps.trader.risk_context import BinanceRiskContexts
from autotrader.apps.trader.shadow_inputs import LiveBinanceInputs
from autotrader.apps.trader.tick import TickContext
from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.models import V6Market
from autotrader.strategies.david_v6.zones import ZONE_HISTORY

TRADE_WINDOW = timedelta(minutes=30)


class ShadowContextSource:
    """The next evaluation, or None when there is nothing new to evaluate."""

    def __init__(
        self,
        *,
        market_data: CompletedBars,
        inputs: LiveBinanceInputs,
        risk: BinanceRiskContexts,
        trade_window: timedelta = TRADE_WINDOW,
    ) -> None:
        self._market_data = market_data
        self._inputs = inputs
        self._risk = risk
        self._trade_window = trade_window
        self._watermark: datetime | None = None

    @property
    def watermark(self) -> datetime | None:
        return self._watermark

    async def context_for(self, now: datetime) -> TickContext | None:
        moment = require_utc(now)
        # Deep enough for the zone builder's ten dates. One kline request
        # returns 5.2 days at this timeframe, which is why the zones were
        # empty on every pass; the depth is stated here rather than assumed
        # there so that the consumer with the longest reach decides it.
        bars = await self._market_data.completed_bars(
            HLIT_TIMEFRAME, moment, history=ZONE_HISTORY
        )
        if not bars:
            return None
        latest = bars[-1].timestamp
        if self._watermark is not None and latest <= self._watermark:
            return None

        risk_context = self._risk.build(bars=bars, now=moment)
        if risk_context is None:
            return None
        daily = await self._market_data.completed_bars(DAILY_TIMEFRAME, moment)
        window_start = moment - self._trade_window
        trades = await self._market_data.trade_prints(window_start, moment)
        built = await self._inputs.build(
            bars=bars,
            daily=daily,
            trades=trades,
            window_start=window_start,
            now=moment,
        )
        if built is None:
            # Something could not be measured this pass. The watermark stays
            # where it was, so the same bar is tried again rather than skipped
            # for good over a window that was briefly too thin.
            return None

        self._watermark = latest
        return TickContext(
            inputs=AssemblyInputs(
                market=V6Market.BINANCE_USDM,
                instrument_id=built.instrument_id,
                decision_at=moment,
                source=AssemblySource(
                    name="BINANCE_USDM_PUBLIC",
                    timezone="UTC",
                    captured_at=moment,
                ),
                bars={"5m": bars, "1d": daily},
                calendar=built.calendar,
                events=built.events,
                benchmark_returns=built.benchmark_returns,
                atr_ratio=built.atr_ratio,
                range_efficiency=built.range_efficiency,
                pessimism=built.pessimism,
                trades=trades,
                order_flow_thresholds=built.order_flow_thresholds,
                fee_schedule=built.fee_schedule,
                spread=built.spread,
                quantity=built.quantity,
                stop_slippage_q95=built.stop_slippage_q95,
                tick_size=built.tick_size,
            ),
            manifest=built.manifest,
            risk_context=risk_context,
            now=moment,
        )


__all__ = ("TRADE_WINDOW", "ShadowContextSource")
