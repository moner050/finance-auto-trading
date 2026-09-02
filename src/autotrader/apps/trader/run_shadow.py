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

import asyncio
import json
import signal
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.bootstrap import master_key_ring
from autotrader.apps.backoffice.provider_secrets import (
    BINANCE_LIVE_REFERENCE,
    MySqlAccountSecretResolver,
)
from autotrader.apps.trader.composition import LeaseSettings, MySqlSchedulerLease
from autotrader.apps.trader.loop import Clock, LoopPorts, SchedulerLease
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
from autotrader.integrations.market_data.binance_session import (
    binance_usdm_calendar,
    session_date_for,
)
from autotrader.integrations.market_data.binance_usdm import BinanceUsdmMarketData
from autotrader.integrations.market_data.economic_calendar import (
    ForexFactoryCalendars,
)
from autotrader.persistence.mysql.models.david_v6 import DavidV6ManifestRow
from autotrader.persistence.mysql.models.operations import OpsIncident
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
    """The ports, the tape the loop reads, and the client to close after."""

    ports: LoopPorts
    rest: BinancePublicRest
    market_data: BinanceUsdmMarketData
    events: ForexFactoryCalendars
    # The same lease the loop holds, handed out so the tape poller can ask the
    # same question with the same instance id. Two halves of one process must
    # be one leader, or the guard would have them fighting each other.
    lease: MySqlSchedulerLease
    equity: Decimal
    tick_size: Decimal


async def run_together(
    *parts: Awaitable[None],
    stop: asyncio.Event,
) -> None:
    """Run the parts of one session, and stop them all when any ends.

    None of them is useful alone. Without the tape every pass quietly produces
    nothing - the inputs cannot rank a delta or an ATR over an empty window -
    so a loop left running would look alive and decide nothing. Without the
    loop the tape fills a table nobody reads. Without the heartbeat the lease
    expires under a process that is still running, and the account is free for
    somebody else to trade while this one is mid-session.

    So whichever ends first ends the rest, and its exception is the run's. A
    correction conflict from the tape has to reach the operator rather than
    leave a loop evaluating a tape that stopped being trustworthy.
    """
    tasks = {asyncio.ensure_future(part) for part in parts}
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for task in done:
        # Raises whichever finished first, if it finished by failing.
        task.result()


# A lease renewed only as often as it is needed is a lease that expires under
# a process that is still running. Renewing at a third of the term leaves room
# for two attempts to be lost before the term does.
LEASE_RENEWAL_FRACTION = 3


# What a lost lease is called where an operator will look for it. The scope is
# the lease rather than the account: one lease governs the loop, and naming the
# account would suggest the account is at fault when another process holds it.
LEASE_LOST = "LEASE_LOST"
LEASE_SCOPE = "LEASE"


class LeaseJournal(Protocol):
    """Where a change of leadership is written down.

    Counting losses in memory and printing them at exit was the whole of the
    record, so a killed process took the count with it and the operations
    screen never learned that another instance had taken the account. The
    lease is the one thing stopping two loops from trading one account; it
    cannot lose that fight quietly.
    """

    async def lost(self, now: datetime) -> None:
        """Leadership was held and is not any more."""
        ...

    async def regained(self, now: datetime) -> None:
        """Leadership came back."""
        ...


class LeaseHeartbeat:
    """Keep the lease alive between passes.

    `run_pass` renews it too, but on the evaluation's cadence, and that cadence
    has nothing to do with the term: five-minute passes against a two-minute
    lease left the account unclaimed for three minutes out of every five, free
    for another instance to take mid-session. Nothing noticed, because the
    tape poller happened to renew every couple of seconds - a correctness
    property propped up by an unrelated component, which is the same defect as
    having none.

    So the renewal cadence is derived from the term rather than configured
    beside it. The two cannot drift apart because there is only one number.
    """

    def __init__(
        self,
        *,
        lease: SchedulerLease,
        clock: Clock,
        ttl: timedelta = LEASE_TTL,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        journal: LeaseJournal | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("the lease term must be positive")
        self._lease = lease
        self._clock = clock
        self._interval = ttl.total_seconds() / LEASE_RENEWAL_FRACTION
        self._sleep = sleep
        self._journal = journal
        # None until the first answer. A process that starts already holding
        # the lease has nothing to report; one that starts without it does,
        # and those are the two cases the first observation has to tell apart.
        self._held: bool | None = None
        self.renewals = 0
        self.losses = 0

    async def run(self, *, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._sleep(self._interval)
            if stop.is_set():
                return
            # Losing it is not a failure to end the run over. Another instance
            # owns the account, `run_pass` will report NOT_LEADER and place
            # nothing, and the poller will store nothing - and leadership can
            # come back, which it cannot if this gave up asking.
            moment = self._clock.now()
            held = await self._lease.acquire(moment)
            if held:
                self.renewals += 1
            else:
                self.losses += 1
            await self._record(held=held, now=moment)

    async def _record(self, *, held: bool, now: datetime) -> None:
        """Write down a change of leadership, and only a change.

        Renewing every forty seconds for six hours is not news; losing the
        account to another process is. Reporting the transition rather than
        the state keeps a three-hour contention to the handful of rows that
        say when it started and when it ended.
        """
        was = self._held
        self._held = held
        if self._journal is None or was == held:
            return
        if not held:
            await self._journal.lost(now)
        elif was is not None:
            await self._journal.regained(now)


class MySqlLeaseJournal:
    """A lost lease as an open incident, resolved when it comes back.

    Open while leadership is elsewhere and resolved when it returns, so the
    operations screen answers "is another process holding my account right
    now" by showing the row or not showing it. That is the same shape the
    protection guard already uses for a position with no stop behind it.

    One row per episode, not one per attempt: `LeaseHeartbeat` only calls
    this on a transition, and opening is skipped when an episode is already
    open in case anything else ever calls it.
    """

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, lease_name: str
    ) -> None:
        if not lease_name or lease_name != lease_name.strip():
            raise ValueError("lease name is required")
        self._sessions = sessions
        self._lease_name = lease_name

    async def lost(self, now: datetime) -> None:
        moment = require_utc(now)
        async with self._sessions() as session:
            if await self._open_episode(session) is not None:
                return
            session.add(
                OpsIncident(
                    severity="WARNING",
                    status="OPEN",
                    reason_code=LEASE_LOST,
                    scope_type=LEASE_SCOPE,
                    scope_key=self._lease_name,
                    created_at=moment,
                )
            )
            await session.commit()

    async def regained(self, now: datetime) -> None:
        require_utc(now)
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(OpsIncident).where(
                        OpsIncident.reason_code == LEASE_LOST,
                        OpsIncident.scope_type == LEASE_SCOPE,
                        OpsIncident.scope_key == self._lease_name,
                        OpsIncident.status == "OPEN",
                    )
                )
            ).all()
            for row in rows:
                row.status = "RESOLVED"
            if rows:
                await session.commit()

    async def _open_episode(self, session: AsyncSession) -> OpsIncident | None:
        return await session.scalar(
            select(OpsIncident).where(
                OpsIncident.reason_code == LEASE_LOST,
                OpsIncident.scope_type == LEASE_SCOPE,
                OpsIncident.scope_key == self._lease_name,
                OpsIncident.status == "OPEN",
            )
        )


# Every way this program is asked to stop. A supervisor sends the first; an
# operator at a terminal sends the second. The third exists only on Windows,
# where a console Ctrl-Break arrives as SIGBREAK rather than as SIGINT -
# leaving it out is why the process died at 0xC000013A with the wind-down
# never reached.
SHUTDOWN_SIGNALS: tuple[signal.Signals, ...] = (
    signal.SIGTERM,
    signal.SIGINT,
    *((signal.SIGBREAK,) if hasattr(signal, "SIGBREAK") else ()),
)


@asynccontextmanager
async def stop_on_signals(
    stop: asyncio.Event,
    *,
    numbers: tuple[signal.Signals, ...] = SHUTDOWN_SIGNALS,
) -> AsyncGenerator[None]:
    """Turn a termination signal into the stop the loop already understands.

    Without this, `timeout` or a supervisor kills the process where it stands.
    Python's default SIGTERM handler does not unwind, so the `finally` that
    reports the run and closes the venue clients never executes, and the last
    thing an operator sees is exit code 124. Worse than losing the summary is
    where it dies: mid-write, between fetching a page of trades and storing
    it, with the checkpoint saying one thing and the tape another.

    Setting the stop instead lets both halves finish what they are doing and
    return, which is the same path a clean end already takes.

    A second signal is not a repeat of the request; it means the first one did
    not work. So the handler puts back whatever was there before - normally
    the default, which kills - and the operator gets their process gone.
    """
    loop = asyncio.get_running_loop()
    previous: dict[signal.Signals, object] = {}

    def request_stop(number: int, frame: object) -> None:
        del frame
        restored = previous.get(signal.Signals(number), signal.SIG_DFL)
        with suppress(OSError, ValueError):
            signal.signal(number, restored)  # type: ignore[arg-type]
        # Thread-safe because it also wakes a loop that is asleep in select;
        # setting the event directly would be seen only once something else
        # happened to wake it.
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(stop.set)

    for number in numbers:
        try:
            previous[number] = signal.signal(number, request_stop)
        except OSError, ValueError:
            # Not the main thread, or a platform without this signal. A run
            # that cannot be asked to stop politely is still a run worth
            # having.
            continue
    try:
        yield
    finally:
        for number, handler in previous.items():
            with suppress(OSError, ValueError):
                signal.signal(number, handler)  # type: ignore[arg-type]


@asynccontextmanager
async def stop_after(
    stop: asyncio.Event, duration: timedelta | None
) -> AsyncGenerator[None]:
    """End the run on its own after `duration`, or never when it is None.

    A run stopped from outside is at the mercy of how it is stopped, and on
    Windows a supervisor cannot ask politely at all: `timeout` and `taskkill`
    terminate through the Win32 API, where no signal is delivered and nothing
    is catchable. A run that knows when it is due to finish does not need to
    be asked.

    The end goes through the same stop a signal sets, so a session that ran to
    its length and one an operator interrupted wind down identically. There is
    only one shutdown path to get right.
    """
    if duration is None:
        yield
        return
    if duration <= timedelta(0):
        raise ValueError("the run duration must be positive")

    async def elapse() -> None:
        await asyncio.sleep(duration.total_seconds())
        stop.set()

    timer = asyncio.ensure_future(elapse())
    try:
        yield
    finally:
        timer.cancel()
        with suppress(asyncio.CancelledError):
            await timer


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


def _session_close_for(moment: datetime) -> datetime:
    """When the session containing `moment` closes.

    Only the monthly employment report needs this: section 8 blocks it for the
    whole session rather than for a fixed number of minutes. A perpetual venue
    has no close of its own, so the boundary is the measured one the rest of
    the system already uses.
    """
    close = binance_usdm_calendar(
        session_date=session_date_for(moment), captured_at=moment
    ).session_close_at
    if close is None:
        # A calendar may report a day the market never opened, and that is a
        # real answer for an exchange with holidays. This venue has none, so
        # the absence would mean the session helper had changed underneath
        # the one rule that depends on it.
        raise ShadowStartupError("the venue calendar reports no session close")
    return close


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
    events = ForexFactoryCalendars(session_close_for=_session_close_for)
    source = ShadowContextSource(
        market_data=market_data,
        inputs=LiveBinanceInputs(
            fixed=fixed,
            spreads=RestSpreads(rest),
            pessimism=StoredPessimism(sessions),
            events=events,
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
    lease = MySqlSchedulerLease(
        sessions,
        LeaseSettings(
            lease_name=LEASE_NAME,
            runtime_instance_id=new_uuid7(),
            ttl=LEASE_TTL,
        ),
    )
    return ShadowLoop(
        market_data=market_data,
        events=events,
        lease=lease,
        ports=shadow_ports(sessions=sessions, source=source, lease=lease),
        rest=rest,
        equity=equity,
        tick_size=specification.tick_size,
    )


__all__ = (
    "LEASE_LOST",
    "LEASE_NAME",
    "LEASE_SCOPE",
    "LEASE_TTL",
    "SHUTDOWN_SIGNALS",
    "SYMBOL",
    "LeaseJournal",
    "MySqlLeaseJournal",
    "RestSpreads",
    "ShadowLoop",
    "ShadowStartupError",
    "StoredPessimism",
    "build_shadow_loop",
    "run_together",
    "stop_after",
    "stop_on_signals",
)
