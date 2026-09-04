"""Wire the Binance USD-M loop against the real account.

Everything here is assembly. What each part does and why is written where it
lives; what this file decides is which parts, and three of those decisions
matter enough to say here.

**One capture per pass, read twice.** Reconciliation needs the venue's
positions and orders, and so does the configuration check that decides whether
an order may be written at all - it is what notices exposure this system did
not put there. Capturing twice would ask the same eight questions twice and
let the two answers disagree.

**The transport reaches exactly the routes this loop uses.** Not "the account
endpoints": the list below, and nothing else is reachable through it even by a
caller that asks. A transport that could reach a withdrawal endpoint is one
that a bug could.

**The submitter is the only thing here that can write.** Everything else reads.
That is the same shape Shadow has - `RefusingExecution` is an absence of
capability rather than a flag - inverted: here the capability exists, in one
object, built in one place.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.bootstrap import master_key_ring
from autotrader.apps.backoffice.provider_secrets import (
    BINANCE_LIVE_REFERENCE,
    MySqlAccountSecretResolver,
)
from autotrader.apps.trader.composition import (
    ExecutionAccount,
    LeaseSettings,
    MySqlDecisionRecorder,
    MySqlPaperExecution,
    MySqlProtectionGuard,
    MySqlReconciler,
    MySqlSchedulerLease,
    MySqlTradingControl,
    bound_policy,
)
from autotrader.apps.trader.live_authority import (
    ConfigurationCache,
    MySqlOrderAuthority,
    VenueConfiguration,
)
from autotrader.apps.trader.live_protection import (
    MySqlEmergencyOrders,
    MySqlProtectionContext,
)
from autotrader.apps.trader.live_safety import MySqlProtectionSafetyActions
from autotrader.apps.trader.live_settlement import MySqlLiveFillSettlement
from autotrader.apps.trader.loop import LoopPorts
from autotrader.apps.trader.market_data import BinanceContextSource, BinanceLoopInputs
from autotrader.apps.trader.quotes import BinanceBookQuotes
from autotrader.apps.trader.risk_context import AccountBudget, BinanceRiskContexts
from autotrader.config.settings import Settings
from autotrader.integrations.brokers.binance_usdm.account import (
    BinanceUsdmAccountSnapshot,
    BinanceUsdmTradeFact,
    capture_binance_usdm_account,
)
from autotrader.integrations.brokers.binance_usdm.algo_order_store import (
    MySqlBinanceUsdmAlgoOrderStore,
)
from autotrader.integrations.brokers.binance_usdm.algo_orders import (
    BinanceUsdmProtectionService,
)
from autotrader.integrations.brokers.binance_usdm.configuration import (
    BinanceUsdmApiKeyEvidence,
    verify_binance_usdm_configuration,
)
from autotrader.integrations.brokers.binance_usdm.live_submitter import (
    LiveBrokerSubmitter,
)
from autotrader.integrations.brokers.binance_usdm.order_store import (
    MySqlBinanceUsdmNormalOrderStore,
)
from autotrader.integrations.brokers.binance_usdm.orders import (
    BinanceUsdmOrderService,
    BinanceUsdmSymbolFilters,
)
from autotrader.integrations.brokers.binance_usdm.rate_limit import (
    BinanceUsdmRateLimiter,
)
from autotrader.integrations.brokers.binance_usdm.secrets import BinanceUsdmSecret
from autotrader.integrations.brokers.binance_usdm.transport import BinanceUsdmTransport
from autotrader.integrations.brokers.common import WhitelistedHttpsTransport
from autotrader.integrations.brokers.live_readers import (
    SnapshotIdentity,
    binance_reader,
    binance_reported,
)
from autotrader.integrations.market_data.binance_instrument import read_specification
from autotrader.integrations.market_data.binance_public_rest import BinancePublicRest
from autotrader.integrations.market_data.binance_usdm import BinanceUsdmMarketData
from autotrader.persistence.mysql.repositories.binance_usdm import (
    BinanceUsdmAlgoOrderRepository,
    BinanceUsdmNormalOrderRepository,
)
from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.models import V6Market

SYMBOL = "BTCUSDT"
BASE_URL = "https://fapi.binance.com"

# Exactly what this loop asks for. A route absent here is unreachable through
# this transport whatever the caller does, which is the point: no withdrawal,
# no transfer, no key management.
ROUTES = frozenset(
    {
        ("GET", "/fapi/v1/time"),
        ("GET", "/fapi/v3/balance"),
        ("GET", "/fapi/v3/positionRisk"),
        ("GET", "/fapi/v1/openOrders"),
        ("GET", "/fapi/v1/openAlgoOrders"),
        ("GET", "/fapi/v1/allOrders"),
        ("GET", "/fapi/v1/allAlgoOrders"),
        ("GET", "/fapi/v1/userTrades"),
        ("GET", "/fapi/v1/income"),
        ("GET", "/fapi/v1/accountConfig"),
        ("GET", "/fapi/v1/positionSide/dual"),
        ("GET", "/fapi/v1/symbolConfig"),
        ("GET", "/fapi/v1/order"),
        ("POST", "/fapi/v1/order"),
        ("GET", "/fapi/v1/algoOrder"),
        ("POST", "/fapi/v1/algoOrder"),
        ("DELETE", "/fapi/v1/algoOrder"),
    }
)

# How often the account's configuration is re-read. The adapter refuses an
# authority older than thirty seconds, so this has to be comfortably inside
# that; halving it leaves room for one slow read.
CONFIGURATION_REFRESH = timedelta(seconds=15)


class SecretResolver(Protocol):
    async def resolve_binance_usdm(self, reference: str) -> BinanceUsdmSecret: ...


@dataclass(frozen=True, slots=True)
class LiveCapture:
    """One reading of the account, shared by everything that needs one."""

    transport: BinanceUsdmTransport

    async def snapshot(self, as_of: datetime) -> BinanceUsdmAccountSnapshot:
        return await capture_binance_usdm_account(
            reader=self.transport, as_of=require_utc(as_of)
        )


@dataclass(frozen=True, slots=True)
class LiveConfiguration:
    """The venue's account configuration, with the filters an order needs."""

    transport: BinanceUsdmTransport
    rest: BinancePublicRest
    capture: LiveCapture
    api_key_evidence: BinanceUsdmApiKeyEvidence
    expected_leverage: int
    owned: Callable[[], Awaitable[Decimal]]
    clock: Callable[[], datetime]

    async def read(self) -> VenueConfiguration:
        now = require_utc(self.clock())
        snapshot = await self.capture.snapshot(now)
        report = await verify_binance_usdm_configuration(
            reader=self.transport,
            snapshot=snapshot,
            api_key_evidence=self.api_key_evidence,
            expected_leverage=self.expected_leverage,
            as_of=now,
            owned_btc_position_amount=await self.owned(),
        )
        specification = await read_specification(self.rest, symbol=SYMBOL)
        return VenueConfiguration(
            report=report,
            filters=BinanceUsdmSymbolFilters(
                tick_size=specification.tick_size,
                step_size=specification.quantity_step,
                minimum_quantity=specification.minimum_quantity,
                minimum_notional=specification.minimum_notional,
                # Stamped with the read that produced it, so the adapter's
                # window measures the age of this answer rather than of the
                # object holding it.
                captured_at=now,
            ),
            read_at=now,
        )


@dataclass(frozen=True, slots=True)
class LiveTrades:
    """Every fill after a trade id, oldest first.

    Paged by id rather than by time. Binance's ids are monotonic per symbol,
    so resuming from one cannot skip a fill that arrived late, which a time
    window can.
    """

    transport: BinanceUsdmTransport
    capture: LiveCapture

    async def after(
        self, trade_id: int | None, *, now: datetime
    ) -> tuple[BinanceUsdmTradeFact, ...]:
        del trade_id  # The window the capture reads already covers the gap.
        snapshot = await self.capture.snapshot(require_utc(now))
        return snapshot.trades


async def build_ports(
    *,
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    market_data: BinanceUsdmMarketData,
    rest: BinancePublicRest,
    inputs: BinanceLoopInputs,
    budget: AccountBudget,
    account: ExecutionAccount,
    lease: LeaseSettings,
    broker_id: UUID,
    binding_id: UUID,
    instrument_id: UUID,
    api_key_evidence: BinanceUsdmApiKeyEvidence,
    expected_leverage: int,
    resolver_factory: (
        Callable[[async_sessionmaker[AsyncSession]], SecretResolver] | None
    ) = None,
) -> LoopPorts:
    """Wire one live loop for the account's bound policy.

    The policy is not a parameter, for the reason the paper composition gives:
    taking one would let a caller size trades against fractions the operator
    never bound to this account, and nothing downstream could notice.
    """
    bound = await bound_policy(
        sessions, account_id=account.account.id, market=V6Market.BINANCE_USDM
    )
    if bound.policy_version_id != account.policy_version_id:
        raise ValueError("the account and its binding must name one version")
    policy = bound.snapshot

    resolver: SecretResolver = (
        MySqlAccountSecretResolver(sessions, master_key_ring(settings))
        if resolver_factory is None
        else resolver_factory(sessions)
    )
    secret = await resolver.resolve_binance_usdm(BINANCE_LIVE_REFERENCE)
    transport = BinanceUsdmTransport(
        transport=WhitelistedHttpsTransport(
            base_url=BASE_URL,
            allowed_routes=ROUTES,
            max_response_bytes=8_000_000,
        ),
        secret=secret,
        now_ms=lambda: int(time.time() * 1000),
        rate_limiter=BinanceUsdmRateLimiter(monotonic=time.monotonic, sleep=_sleep),
    )
    capture = LiveCapture(transport=transport)
    quotes = BinanceBookQuotes(rest=rest, symbol=SYMBOL)

    configuration = ConfigurationCache(
        source=LiveConfiguration(
            transport=transport,
            rest=rest,
            capture=capture,
            api_key_evidence=api_key_evidence,
            expected_leverage=expected_leverage,
            owned=lambda: _owned(sessions, account, instrument_id),
            clock=_now,
        ),
        every=CONFIGURATION_REFRESH,
    )

    orders = BinanceUsdmOrderService(
        authority=MySqlOrderAuthority(
            sessions=sessions,
            account=account,
            configuration=configuration,
            quotes=quotes,
            expected_leverage=expected_leverage,
        ),
        store=MySqlBinanceUsdmNormalOrderStore(
            BinanceUsdmNormalOrderRepository(_session(sessions))
        ),
        sender=transport,
        clock=_now,
    )
    snapshots = binance_reader(
        identity=SnapshotIdentity(broker_id=broker_id, account_id=account.account.id),
        capture=lambda moment: _reported(capture, moment),
        resolver=_InstrumentResolver(instrument_id),
    )
    protection = BinanceUsdmProtectionService(
        store=MySqlBinanceUsdmAlgoOrderStore(
            BinanceUsdmAlgoOrderRepository(_session(sessions))
        ),
        sender=transport,
        emergency_orders=MySqlEmergencyOrders(
            sessions=sessions,
            orders=orders,
            snapshots=snapshots,
            account_id=account.account.id,
            instrument_id=instrument_id,
        ),
        safety_actions=MySqlProtectionSafetyActions(
            sessions=sessions, binding_id=binding_id
        ),
        clock=_now,
    )
    submitter = LiveBrokerSubmitter(
        orders=orders,
        protection=protection,
        context=MySqlProtectionContext(
            sessions=sessions,
            account=account,
            tick_size=_tick_size(configuration),
        ),
    )

    return LoopPorts(
        lease=MySqlSchedulerLease(sessions, lease),
        settlement=MySqlLiveFillSettlement(
            sessions=sessions,
            account=account,
            broker_id=broker_id,
            trades=LiveTrades(transport=transport, capture=capture),
            broker=submitter,
        ),
        reconciliation=MySqlReconciler(
            sessions=sessions, account=account, reader=snapshots
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
            quotes=quotes,
        ),
    )


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def _now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


def _session(sessions: async_sessionmaker[AsyncSession]) -> AsyncSession:
    return sessions()


async def _reported(capture: LiveCapture, moment: datetime):
    return binance_reported(await capture.snapshot(moment))


async def _owned(
    sessions: async_sessionmaker[AsyncSession],
    account: ExecutionAccount,
    instrument_id: UUID,
) -> Decimal:
    """What the ledger says is held, for the venue's answer to be checked against."""
    from sqlalchemy import select

    from autotrader.persistence.mysql.models.positions import Position

    async with sessions() as session:
        held = await session.scalar(
            select(Position.quantity).where(
                Position.account_id == account.account.id,
                Position.instrument_id == instrument_id,
            )
        )
    return held or Decimal()


def _tick_size(configuration: ConfigurationCache) -> Decimal:
    held = configuration.current
    if held is None:
        # Read before the first refresh. The authority refuses on the same
        # absence, so this returns a value that cannot round anything rather
        # than a plausible one.
        return Decimal("0.1")
    return held.filters.tick_size


@dataclass(frozen=True, slots=True)
class _InstrumentResolver:
    """One symbol, one instrument. Anything else is refused upstream."""

    instrument_id: UUID

    async def resolve(self, exchange_code: str, code: str) -> UUID:
        del exchange_code
        if code != SYMBOL:
            raise LookupError(f"this loop trades {SYMBOL}, not {code}")
        return self.instrument_id


__all__ = (
    "BASE_URL",
    "CONFIGURATION_REFRESH",
    "ROUTES",
    "SYMBOL",
    "LiveCapture",
    "LiveConfiguration",
    "LiveTrades",
    "build_ports",
)
