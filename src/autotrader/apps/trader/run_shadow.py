"""Assemble a Shadow loop from what the venue and the database already know.

Every credentialed call this program makes happens here, before the loop
starts, and the loop receives values. Reading a commission rate and a wallet
balance needs a key that can also place orders; a loop holding that key would
be one import from being able to use it, and the whole point of Shadow is that
it cannot.

The three reads are read-only: `exchangeInfo` and `bookTicker` are public,
`commissionRate` and `balance` are signed. Nothing here submits.

What is not read is the leverage. It is not on a position the venue reports
and it decides the size of an order, so it is stated on the command line or
the program refuses - the same rule `OperatorFacts` states for the money.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.bootstrap import master_key_ring
from autotrader.apps.backoffice.provider_secrets import (
    BINANCE_LIVE_REFERENCE,
    MySqlAccountSecretResolver,
)
from autotrader.apps.trader.composition import LeaseSettings, MySqlSchedulerLease
from autotrader.apps.trader.loop import LoopPorts
from autotrader.apps.trader.risk_context import AccountBudget, BinanceRiskContexts
from autotrader.apps.trader.shadow import shadow_ports
from autotrader.apps.trader.shadow_inputs import FixedFacts, LiveBinanceInputs
from autotrader.apps.trader.shadow_source import ShadowContextSource
from autotrader.apps.trader.startup import ResolvedAccount
from autotrader.config.settings import Settings
from autotrader.integrations.brokers.binance_usdm.account import (
    BinanceUsdmAccountReader,
)
from autotrader.integrations.market_data.binance_instrument import (
    read_specification,
    read_spread,
)
from autotrader.integrations.market_data.binance_public_rest import BinancePublicRest
from autotrader.integrations.market_data.binance_usdm import BinanceUsdmMarketData
from autotrader.persistence.mysql.models.david_v6 import DavidV6ManifestRow
from autotrader.persistence.mysql.repositories.market_tape import MySqlMarketTape
from autotrader.persistence.mysql.repositories.pessimism import MarketPessimism
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.costs import FeeSchedule
from autotrader.strategies.david_v6.manifest import V6Manifest
from autotrader.strategies.david_v6.regime import PessimismInputs

SYMBOL = "BTCUSDT"
LEASE_NAME = "binance-usdm-shadow"
LEASE_TTL = timedelta(minutes=2)


class ShadowStartupError(RuntimeError):
    """Raised when the loop cannot be assembled, saying which part."""


class RestSpreads:
    """The best bid and ask, re-read every pass over the public API."""

    def __init__(self, rest: BinancePublicRest) -> None:
        self._rest = rest

    async def spread(self) -> Decimal:
        return (await read_spread(self._rest, symbol=SYMBOL)).spread


class StoredPessimism:
    """The day's percentiles, out of the table the capture fills."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def pessimism(self, *, through: date) -> PessimismInputs:
        async with self._sessions() as session:
            return await MarketPessimism(session).pessimism(through=through)


@dataclass(frozen=True, slots=True)
class ShadowLoop:
    """The ports, and the client that has to be closed after."""

    ports: LoopPorts
    rest: BinancePublicRest
    equity: Decimal
    tick_size: Decimal


async def _manifest(
    sessions: async_sessionmaker[AsyncSession], resolved: ResolvedAccount
) -> V6Manifest:
    async with sessions() as session:
        row = await session.get(DavidV6ManifestRow, resolved.manifest_id)
        if row is None:
            raise ShadowStartupError("the resolved manifest is not stored")
        # Read out before the session goes away; a detached row raises on
        # attribute access rather than returning what it last held.
        manifest = V6Manifest(
            id=row.id,
            strategy_version_id=row.strategy_version_id,
            source_sha256=row.source_sha256,
            design_sha256=row.design_sha256,
            configuration_hash=row.configuration_hash,
            registered_at=require_utc(row.registered_at),
        )
        await session.rollback()
    return manifest


async def _wallet_and_fee(
    settings: Settings, sessions: async_sessionmaker[AsyncSession], *, price: Decimal
) -> tuple[Decimal, FeeSchedule]:
    """The account's USDT balance and the fee schedule at this price.

    Both need the key, so both are read once, here. Imported inside the
    function so that merely importing this module does not pull a broker
    transport into anything that only wanted the ports.
    """
    import asyncio
    import time

    from autotrader.integrations.brokers.binance_usdm.rate_limit import (
        BinanceUsdmRateLimiter,
    )
    from autotrader.integrations.brokers.binance_usdm.transport import (
        BinanceUsdmTransport,
    )
    from autotrader.integrations.brokers.common import WhitelistedHttpsTransport
    from autotrader.integrations.market_data.binance_commission import (
        fee_schedule_for,
        read_commission_rates,
    )

    resolver = MySqlAccountSecretResolver(sessions, master_key_ring(settings))
    secret = await resolver.resolve_binance_usdm(BINANCE_LIVE_REFERENCE)
    transport = BinanceUsdmTransport(
        transport=WhitelistedHttpsTransport(
            base_url="https://fapi.binance.com",
            # Read-only, and exactly the routes these two answers need. An
            # order path is unreachable rather than merely unused.
            allowed_routes=frozenset(
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
                    ("GET", "/fapi/v1/commissionRate"),
                }
            ),
            max_response_bytes=8_000_000,
        ),
        secret=secret,
        now_ms=lambda: int(time.time() * 1000),
        rate_limiter=BinanceUsdmRateLimiter(
            monotonic=time.monotonic, sleep=asyncio.sleep
        ),
    )
    balance = await _usdt_balance(transport)
    rates = await read_commission_rates(transport, symbol=SYMBOL)
    return balance, fee_schedule_for(rates, price=price)


async def _usdt_balance(transport: BinanceUsdmAccountReader) -> Decimal:
    """The USD-M wallet balance, read on its own.

    The full account capture would answer this too, and it fetches eight
    endpoints to do it and reports any one of them failing as "snapshot is
    incomplete" with the cause discarded. One question, one endpoint: what
    goes wrong here says what went wrong.
    """
    from autotrader.integrations.brokers.common import BrokerRequest

    response = await transport.send(
        BrokerRequest(method="GET", path="/fapi/v3/balance")
    )
    if response.status != 200:
        raise ShadowStartupError(f"the balance request answered {response.status}")
    try:
        payload = json.loads(response.body)
    except ValueError as error:
        raise ShadowStartupError("the balance response is not JSON") from error
    if not isinstance(payload, list):
        raise ShadowStartupError("the balance response is not a list")
    for entry in cast("list[object]", payload):
        if not isinstance(entry, dict):
            continue
        row = cast("dict[str, object]", entry)
        if row.get("asset") != "USDT":
            continue
        try:
            return Decimal(str(row["balance"]))
        except (KeyError, TypeError, InvalidOperation) as error:
            raise ShadowStartupError("the USDT balance is unreadable") from error
    raise ShadowStartupError("the account reports no USDT balance")


async def build_shadow_loop(
    *,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    resolved: ResolvedAccount,
    policy: object,
    leverage: int,
) -> ShadowLoop:
    """Everything the loop needs, read once."""
    if type(leverage) is not int or leverage <= 0:
        raise ShadowStartupError("leverage must be a positive integer")

    manifest = await _manifest(sessions, resolved)
    rest = BinancePublicRest()
    specification = await read_specification(rest, symbol=SYMBOL)
    book = await read_spread(rest, symbol=SYMBOL)
    mid = (book.best_bid + book.best_ask) / 2

    equity, schedule = await _wallet_and_fee(settings, sessions, price=mid)
    if equity <= 0:
        raise ShadowStartupError(
            "the futures wallet holds nothing; the loop would size every "
            "trade against zero equity"
        )

    assert schedule.entry_fee_per_unit is not None
    assert schedule.exit_taker_fee_per_unit is not None
    # What one unit costs to open and close, which is what the risk request
    # prices a trade against.
    cost_per_unit = schedule.entry_fee_per_unit + schedule.exit_taker_fee_per_unit

    fixed = FixedFacts(
        instrument_id=resolved.instrument_id,
        manifest=manifest,
        fee_schedule=schedule,
        tick_size=specification.tick_size,
        minimum_quantity=specification.minimum_quantity,
    )
    market_data = BinanceUsdmMarketData(rest=rest, store=MySqlMarketTape(sessions))
    source = ShadowContextSource(
        market_data=market_data,
        inputs=LiveBinanceInputs(
            fixed=fixed,
            spreads=RestSpreads(rest),
            pessimism=StoredPessimism(sessions),
        ),
        risk=BinanceRiskContexts(
            budget=AccountBudget(
                # Shadow places nothing, so the session cannot move the
                # balance. The instant it opened with is the session's start.
                session_start_equity=equity,
                current_equity=equity,
                quantity_step=specification.quantity_step,
                tick_size=specification.tick_size,
                spread=book.spread,
                cost_per_unit=cost_per_unit,
                leverage=leverage,
            ),
            policy=policy,  # type: ignore[arg-type]
        ),
    )
    return ShadowLoop(
        ports=shadow_ports(
            sessions=sessions,
            source=source,
            lease=MySqlSchedulerLease(
                sessions,
                LeaseSettings(
                    lease_name=LEASE_NAME,
                    runtime_instance_id=new_uuid7(),
                    ttl=LEASE_TTL,
                ),
            ),
        ),
        rest=rest,
        equity=equity,
        tick_size=specification.tick_size,
    )


__all__ = (
    "LEASE_NAME",
    "LEASE_TTL",
    "SYMBOL",
    "RestSpreads",
    "ShadowLoop",
    "ShadowStartupError",
    "StoredPessimism",
    "build_shadow_loop",
)
