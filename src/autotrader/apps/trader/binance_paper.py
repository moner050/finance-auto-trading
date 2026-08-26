"""Run the Binance USD-M paper loop.

Everything here is wiring. The account, the fees and the risk budget come from
the caller because they belong to an operator, not to a strategy, and the loop
refuses to start rather than guess at any of them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.trader.composition import (
    ExecutionAccount,
    LeaseSettings,
    MySqlDecisionRecorder,
    MySqlFillSettlement,
    MySqlPaperExecution,
    MySqlSchedulerLease,
    MySqlTradingControl,
)
from autotrader.apps.trader.loop import (
    Clock,
    LoopPass,
    LoopPorts,
    SystemClock,
    run_forever,
    run_pass,
)
from autotrader.apps.trader.market_data import (
    HLIT_TIMEFRAME,
    BinanceContextSource,
    BinanceExecutionBars,
    BinanceLoopInputs,
)
from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import OrderStyle, Side
from autotrader.integrations.brokers.internal_paper import (
    PaperOrderCommand,
    PaperOrderReceipt,
)
from autotrader.integrations.brokers.paper_submitter import (
    PaperAccount,
    PaperBrokerSubmitter,
)
from autotrader.integrations.market_data.binance_public_rest import BinancePublicRest
from autotrader.integrations.market_data.binance_usdm import BinanceUsdmMarketData
from autotrader.persistence.mysql.paper_journal import MySqlPaperJournal
from autotrader.persistence.mysql.repositories.core import (
    CoreInstrumentRegistry,
    InstrumentListing,
)
from autotrader.persistence.mysql.seeds.core import (
    BINANCE_USDM_EXCHANGE_CODE,
    seed_core_reference_session,
)
from autotrader.risk.v6 import V6RiskContext, V6RiskRequest
from autotrader.shared.ids import new_uuid7
from autotrader.strategies.david_v6.models import SetupGrade, StrategyFamily, V6Market

PAPER_ALIAS = "internal-binance-usdm-paper"
_ATR_WINDOW = 14

BTCUSDT = InstrumentListing(
    exchange_code=BINANCE_USDM_EXCHANGE_CODE,
    code="BTCUSDT",
    name="BTCUSDT Perpetual",
    instrument_type="PERPETUAL",
)


@dataclass(frozen=True, slots=True)
class AccountBudget:
    """The operator's money, which no strategy may invent."""

    session_start_equity: Decimal
    current_equity: Decimal
    quantity_step: Decimal
    tick_size: Decimal
    spread: Decimal
    cost_per_unit: Decimal
    leverage: int
    valid_for: timedelta = timedelta(minutes=5)


class BinanceRiskContexts:
    """Price the account for one evaluation, from the bars it just saw."""

    def __init__(self, *, budget: AccountBudget, side: Side = Side.BUY) -> None:
        self._budget = budget
        self._side = side

    def build(
        self, *, bars: tuple[CompletedOhlcvBar, ...], now: datetime
    ) -> V6RiskContext | None:
        atr = _average_true_range(bars)
        if atr is None:
            return None
        budget = self._budget
        entry = bars[-1].close
        return V6RiskContext(
            decision_id=new_uuid7(),
            setup_id=new_uuid7(),
            feature_snapshot_id=new_uuid7(),
            family=StrategyFamily.HLIT,
            order_style=OrderStyle.LIMIT,
            # The tick replaces both from the assembled evidence.
            matched_indicators=(),
            mandatory_indicator_codes=frozenset(),
            risk_request=V6RiskRequest(
                market=V6Market.BINANCE_USDM,
                grade=SetupGrade.NORMAL,
                side=self._side,
                entry_price=entry,
                # A placeholder the exhaustion overrides once it is confirmed.
                structural_reference=(
                    entry - atr if self._side is Side.BUY else entry + atr
                ),
                tick_size=budget.tick_size,
                spread=budget.spread,
                atr_30s=atr,
                atr_5m=atr,
                session_start_equity=budget.session_start_equity,
                current_equity=budget.current_equity,
                daily_net_pnl=Decimal(0),
                weekly_net_pnl=Decimal(0),
                consecutive_net_losses=0,
                current_open_structural_risk=Decimal(0),
                quantity_step=budget.quantity_step,
                cost_per_unit=budget.cost_per_unit,
                leverage=budget.leverage,
            ),
            target_price=entry,
            valid_until=now + budget.valid_for,
        )


def _average_true_range(
    bars: tuple[CompletedOhlcvBar, ...], window: int = _ATR_WINDOW
) -> Decimal | None:
    if len(bars) <= window:
        return None
    ranges: list[Decimal] = []
    for previous, current in zip(bars[-window - 1 : -1], bars[-window:], strict=True):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    average = sum(ranges, start=Decimal(0)) / Decimal(len(ranges))
    return average if average > 0 else None


async def register_instruments(
    sessions: async_sessionmaker[AsyncSession],
) -> UUID:
    """Seed the fixed reference rows, register BTCUSDT, and return its id.

    The loop has to name a canonical instrument in every decision it records.
    Reading that id back from the registry is the only way a caller cannot
    invent one that no table has ever heard of.
    """
    async with sessions() as session:
        await seed_core_reference_session(session)
        instrument_id = await CoreInstrumentRegistry(session).register(BTCUSDT)
        await session.commit()
    return instrument_id


def build_ports(
    *,
    sessions: async_sessionmaker[AsyncSession],
    market_data: BinanceUsdmMarketData,
    inputs: BinanceLoopInputs,
    budget: AccountBudget,
    account: ExecutionAccount,
    lease: LeaseSettings,
) -> LoopPorts:
    bars = BinanceExecutionBars(market_data)
    paper = PaperAccount(
        account_alias=PAPER_ALIAS,
        market=V6Market.BINANCE_USDM,
        timeframe=HLIT_TIMEFRAME,
        fee_per_unit=budget.cost_per_unit,
        slippage_per_unit=budget.spread,
    )

    return LoopPorts(
        lease=MySqlSchedulerLease(sessions, lease),
        settlement=MySqlFillSettlement(sessions=sessions, bars=bars),
        source=BinanceContextSource(
            market_data=market_data,
            inputs=inputs,
            risk=BinanceRiskContexts(budget=budget),
        ),
        control=MySqlTradingControl(sessions),
        recorder=MySqlDecisionRecorder(sessions),
        execution=MySqlPaperExecution(
            sessions=sessions,
            account=account,
            broker=PaperBrokerSubmitter(
                journal=SessionPaperJournal(sessions), account=paper
            ),
        ),
    )


class SessionPaperJournal:
    """Open a short session per journal call, since dispatch owns its own."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def load_receipt(self, command_id: object) -> PaperOrderReceipt | None:
        async with self._sessions() as session:
            return await MySqlPaperJournal(session).load_receipt(command_id)

    async def stage_command(self, command: PaperOrderCommand, digest: bytes) -> None:
        async with self._sessions() as session:
            await MySqlPaperJournal(session).stage_command(command, digest)
            await session.commit()

    async def persist_receipt(self, receipt: PaperOrderReceipt) -> None:
        async with self._sessions() as session:
            await MySqlPaperJournal(session).persist_receipt(receipt)
            await session.commit()

    async def unresolved_commands(
        self, *, order_id: UUID | None = None
    ) -> tuple[PaperOrderCommand, ...]:
        async with self._sessions() as session:
            return await MySqlPaperJournal(session).unresolved_commands(
                order_id=order_id
            )


async def open_market_data(
    store: object,
) -> tuple[BinanceUsdmMarketData, BinancePublicRest]:
    rest = BinancePublicRest()
    return BinanceUsdmMarketData(rest=rest, store=store), rest  # type: ignore[arg-type]


async def run(
    *,
    ports: LoopPorts,
    interval: timedelta = HLIT_TIMEFRAME,
    stop: asyncio.Event | None = None,
) -> None:
    await run_forever(
        ports=ports,
        clock=SystemClock(),
        interval=interval,
        stop=stop or asyncio.Event(),
    )


async def run_one(ports: LoopPorts, *, clock: Clock | None = None) -> LoopPass:
    """One pass, on the same clock the running loop uses."""
    return await run_pass(now=(clock or SystemClock()).now(), ports=ports)


__all__ = (
    "BTCUSDT",
    "PAPER_ALIAS",
    "AccountBudget",
    "BinanceRiskContexts",
    "SessionPaperJournal",
    "build_ports",
    "open_market_data",
    "register_instruments",
    "run",
    "run_one",
)
