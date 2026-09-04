"""Run the Binance USD-M paper loop.

Everything here is wiring. The account, the fees and the risk budget come from
the caller because they belong to an operator, not to a strategy, and the loop
refuses to start rather than guess at any of them.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.trader.composition import (
    ExecutionAccount,
    LeaseSettings,
    MySqlDecisionRecorder,
    MySqlFillSettlement,
    MySqlPaperExecution,
    MySqlPaperSnapshotReader,
    MySqlProtectionGuard,
    MySqlReconciler,
    MySqlSchedulerLease,
    MySqlTradingControl,
    bound_policy,
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
from autotrader.apps.trader.quotes import BinanceBookQuotes
from autotrader.apps.trader.risk_context import AccountBudget, BinanceRiskContexts
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
from autotrader.strategies.david_v6.models import V6Market

PAPER_ALIAS = "internal-binance-usdm-paper"

BTCUSDT = InstrumentListing(
    exchange_code=BINANCE_USDM_EXCHANGE_CODE,
    code="BTCUSDT",
    name="BTCUSDT Perpetual",
    instrument_type="PERPETUAL",
)


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


async def build_ports(
    *,
    sessions: async_sessionmaker[AsyncSession],
    market_data: BinanceUsdmMarketData,
    rest: BinancePublicRest,
    inputs: BinanceLoopInputs,
    budget: AccountBudget,
    account: ExecutionAccount,
    lease: LeaseSettings,
) -> LoopPorts:
    """Wire one loop for the account's bound policy.

    The policy is not a parameter. Taking one would let a caller size trades
    against fractions the operator never bound to this account, and nothing
    downstream could notice.
    """
    bound = await bound_policy(
        sessions, account_id=account.account.id, market=V6Market.BINANCE_USDM
    )
    if bound.policy_version_id != account.policy_version_id:
        # A decision measured against one policy and filed under another is
        # unauditable.
        raise ValueError("the account and its binding must name one version")
    policy = bound.snapshot
    bars = BinanceExecutionBars(market_data)
    paper = PaperAccount(
        account_alias=PAPER_ALIAS,
        market=V6Market.BINANCE_USDM,
        timeframe=HLIT_TIMEFRAME,
        fee_per_unit=budget.cost_per_unit,
        slippage_per_unit=budget.spread,
    )

    # One broker for both halves: the entry goes through it, and so does the
    # stop that settlement places once the entry fills.
    submitter = PaperBrokerSubmitter(
        journal=SessionPaperJournal(sessions), account=paper
    )

    return LoopPorts(
        lease=MySqlSchedulerLease(sessions, lease),
        settlement=MySqlFillSettlement(
            sessions=sessions, bars=bars, account=account, broker=submitter
        ),
        reconciliation=MySqlReconciler(
            sessions=sessions,
            account=account,
            reader=MySqlPaperSnapshotReader(sessions=sessions, account=account),
        ),
        protection=MySqlProtectionGuard(sessions=sessions, account=account),
        source=BinanceContextSource(
            market_data=market_data,
            inputs=inputs,
            risk=BinanceRiskContexts(budget=budget, policy=policy),
        ),
        control=MySqlTradingControl(sessions),
        recorder=MySqlDecisionRecorder(sessions),
        execution=MySqlPaperExecution(
            sessions=sessions,
            account=account,
            broker=submitter,
            # The entry is a market order, so its intent needs the price it
            # will get. §31.11.
            quotes=BinanceBookQuotes(rest=rest, symbol=market_data.symbol),
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

    async def staged_command(self, command_id: UUID) -> PaperOrderCommand | None:
        async with self._sessions() as session:
            return await MySqlPaperJournal(session).staged_command(command_id)

    async def void_and_stage(
        self,
        *,
        voided: PaperOrderReceipt,
        staged: PaperOrderCommand,
        digest: bytes,
    ) -> None:
        # One session, so the void and the new order commit together. The
        # gap between them is a position with no stop behind it.
        async with self._sessions() as session:
            await MySqlPaperJournal(session).void_and_stage(
                voided=voided, staged=staged, digest=digest
            )
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
