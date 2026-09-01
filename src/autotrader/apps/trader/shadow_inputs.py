"""The loop's inputs, rebuilt from the venue on every pass.

`BinanceContextSource` takes one `BinanceLoopInputs` and holds it for the life
of the run. That was right when the values came from a test fixture and wrong
now that most of them are measurements: the spread, the modelled stop
slippage, the thirty-second ATR and the pessimism percentiles all move while
a session runs, and a loop evaluating a bar from an hour ago against an
hour-old spread is deciding on a market that is not there.

What is fixed is fixed here too, and deliberately so. The instrument, the
manifest and the fee schedule are read once before the loop starts: the first
two identify what is being traded and under which build, and re-reading them
mid-run would mean a session whose decisions were filed under two different
answers. The fee is read once because reading it needs credentials, and the
loop must not hold any - it receives the resulting schedule as a value.

Anything that cannot be measured this pass makes the pass produce nothing.
There is no partial input: the strategy's own evidence machinery already
refuses on what it does not have, but assembling half a set here and calling
it complete would put a decision on record that was measured against a gap.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from autotrader.apps.trader.market_data import BinanceLoopInputs
from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.integrations.market_data.binance_session import (
    binance_usdm_calendar,
    session_date_for,
)
from autotrader.strategies.david_v6.costs import FeeSchedule, stop_slippage_from_bars
from autotrader.strategies.david_v6.manifest import V6Manifest
from autotrader.strategies.david_v6.order_flow import (
    OrderFlowThresholds,
    TradePrint,
    thirty_second_atr,
)
from autotrader.strategies.david_v6.regime import PessimismInputs, daily_returns


@dataclass(frozen=True, slots=True)
class FixedFacts:
    """What is read once, before the loop starts.

    The instrument and the manifest name what is traded and under which build,
    and a session whose decisions were filed under two answers to either is a
    session nobody can read. The fee schedule is here because reading it needs
    an authenticated call, and the loop holds no credentials.
    """

    instrument_id: UUID
    manifest: V6Manifest
    fee_schedule: FeeSchedule
    tick_size: Decimal
    minimum_quantity: Decimal


class PessimismSource(Protocol):
    """Whatever can answer the day's percentiles."""

    async def pessimism(self, *, through: date) -> PessimismInputs: ...


class SpreadSource(Protocol):
    """The current best bid and ask, as a distance."""

    async def spread(self) -> Decimal: ...


class LiveBinanceInputs:
    """Assemble one pass's inputs, or refuse the pass."""

    def __init__(
        self,
        *,
        fixed: FixedFacts,
        spreads: SpreadSource,
        pessimism: PessimismSource,
    ) -> None:
        self._fixed = fixed
        self._spreads = spreads
        self._pessimism = pessimism

    async def build(
        self,
        *,
        bars: Sequence[CompletedOhlcvBar],
        daily: Sequence[CompletedOhlcvBar],
        trades: Sequence[TradePrint],
        window_start: datetime,
        now: datetime,
    ) -> BinanceLoopInputs | None:
        fixed = self._fixed
        atr_30s = thirty_second_atr(trades, window_start=window_start, window_end=now)
        if atr_30s is None:
            # The order-flow rules measure progress against it; without one
            # they would be comparing against zero.
            return None
        slippage = stop_slippage_from_bars(bars)
        if slippage is None:
            return None
        closes = tuple(bar.close for bar in daily)
        if len(closes) < 2:
            return None

        return BinanceLoopInputs(
            instrument_id=fixed.instrument_id,
            manifest=fixed.manifest,
            calendar=binance_usdm_calendar(
                session_date=session_date_for(now), captured_at=now
            ),
            order_flow_thresholds=OrderFlowThresholds(
                tick_size=fixed.tick_size, atr_30s=atr_30s
            ),
            fee_schedule=fixed.fee_schedule,
            tick_size=fixed.tick_size,
            spread=await self._spreads.spread(),
            stop_slippage_q95=slippage,
            # The venue's smallest order. Only the reported round-trip total
            # scales with this; every per-unit number a decision reads does
            # not, and the size an order would actually take is the risk
            # engine's answer rather than one assumed here.
            quantity=fixed.minimum_quantity,
            pessimism=await self._pessimism.pessimism(through=now.date()),
            benchmark_returns=daily_returns(closes),
        )


__all__ = (
    "FixedFacts",
    "LiveBinanceInputs",
    "PessimismSource",
    "SpreadSource",
)
