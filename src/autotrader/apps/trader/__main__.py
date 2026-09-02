"""Start the trader in Shadow, or say exactly why it cannot start.

    python -m autotrader.apps.trader --account <alias> --check
    python -m autotrader.apps.trader --account <alias> --run --shadow --leverage <n>

`--check` resolves everything and reports without connecting to a venue.

`--run` needs `--shadow` spelled out. It is the only mode that exists, and a
`--run` that quietly chose one would be a flag away from choosing another;
Paper and LIVE are sections 11.7 and 11.8, behind two Shadow and two Paper
sessions. The Shadow loop evaluates real bars and records real decisions
through an execution port with no broker behind it.

`--for` ends the run on its own after a stated length, as in `--for 6h`.
Without it the run continues until it is stopped. On Windows there is no
polite way to stop it from outside - `timeout` and `taskkill` terminate
through the Win32 API, where no signal is delivered - so a run that has to
finish cleanly states its own length.

`--leverage` is required. It is on no position the venue reports and it
decides the size of an order, so it is stated rather than guessed - the rule
`OperatorFacts` states for the money.

What is left without a producer blocks nothing. `range_efficiency` and
`atr_ratio` are observations section 2.1's regime does not consult, so they
are reported as absent rather than as reasons.

Only Binance USD-M is wired. It is the one composition complete end to end;
an entry point offering all three markets would refuse for two of them and
teach nobody anything by doing so.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.apps.trader.binance_paper import BTCUSDT
from autotrader.apps.trader.market_data import HLIT_TIMEFRAME
from autotrader.apps.trader.startup import (
    StartupRefusedError,
    resolve_account,
    unsourced_inputs,
)
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.seeds.core import BINANCE_USDM_EXCHANGE_CODE
from autotrader.strategies.david_v6.models import V6Market

_USAGE_LINES = (
    "usage: python -m autotrader.apps.trader --account <alias> --check",
    "   or: python -m autotrader.apps.trader --account <alias> --run "
    "--shadow --leverage <n> [--for <30m|6h|900s>]",
)
USAGE = "\n".join(_USAGE_LINES)
MARKET = V6Market.BINANCE_USDM


# Seconds, minutes, hours. A bare number is refused rather than guessed at:
# `--for 30` means half a minute to one operator and half an hour to another,
# and the two differ by a factor of sixty on a live account.
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600}


def parse_duration(text: str) -> timedelta:
    """`90m`, `6h`, `900s`. Anything else is refused."""
    if type(text) is not str or not text:
        raise ValueError("a duration needs a number and a unit, as in 6h")
    if text.isdigit():
        # The whole point of the unit. Reporting this as an unknown unit of
        # `0` would send the operator looking at the wrong character.
        raise ValueError(f"{text!r} needs a number and a unit, as in {text}m")
    number, unit = text[:-1], text[-1]
    seconds = _DURATION_UNITS.get(unit)
    if seconds is None:
        raise ValueError(f"unknown duration unit {unit!r}; use s, m or h")
    if not number.isdigit():
        raise ValueError(f"{number!r} is not a whole number of {unit!r}")
    total = int(number) * seconds
    if total <= 0:
        raise ValueError("a duration must be positive")
    return timedelta(seconds=total)


def _value(argv: tuple[str, ...], flag: str) -> str | None:
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            return argv[index + 1]
    return None


async def _run_shadow(alias: str, leverage: int, run_for: timedelta | None) -> int:
    """Evaluate real bars and record real decisions, placing nothing."""
    from autotrader.apps.trader.composition import bound_policy
    from autotrader.apps.trader.loop import SystemClock, run_forever
    from autotrader.apps.trader.run_shadow import (
        LEASE_NAME,
        LeaseHeartbeat,
        MySqlLeaseJournal,
        ShadowStartupError,
        build_shadow_loop,
        run_together,
        stop_after,
        stop_on_signals,
    )
    from autotrader.apps.trader.shadow import SHADOW
    from autotrader.integrations.market_data.binance_trade_poller import (
        POLL_INTERVAL_SECONDS,
        BinanceUsdmTradePoller,
    )
    from autotrader.integrations.market_data.economic_calendar import FEED_URL

    settings = Settings()
    engine = create_engine(settings)
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        try:
            resolved = await resolve_account(
                sessions,
                account_alias=alias,
                market=MARKET,
                instrument_code=BTCUSDT.code,
                exchange_code=BINANCE_USDM_EXCHANGE_CODE,
            )
            bound = await bound_policy(
                sessions, account_id=resolved.account.id, market=MARKET
            )
            loop = await build_shadow_loop(
                settings=settings,
                sessions=sessions,
                resolved=resolved,
                policy=bound.snapshot,
                leverage=leverage,
            )
        except (StartupRefusedError, ShadowStartupError) as error:
            print(str(error), file=sys.stderr)
            return 1

        # Fetched rather than pushed: `btcusdt@aggTrade` accepts a
        # subscription and then sends nothing, while this endpoint returns the
        # same rows with the same ids. See binance_trade_poller.
        tape = BinanceUsdmTradePoller(
            market_data=loop.market_data,
            rest=loop.rest,
            # The same lease the evaluation holds. The tape is one table
            # shared by every instance on this database, and two pollers
            # resuming from one checkpoint fetch the same page and insert it
            # twice.
            lease=loop.lease,
            clock=SystemClock(),
        )
        heartbeat = LeaseHeartbeat(
            lease=loop.lease,
            clock=SystemClock(),
            # Losing the account to another instance used to be an in-memory
            # count printed at exit, which a killed process took with it and
            # the operations screen never saw.
            journal=MySqlLeaseJournal(sessions, lease_name=LEASE_NAME),
        )
        print(f"mode          {SHADOW}")
        print(f"account       {alias} ({resolved.account.environment})")
        print(f"equity        {loop.equity} USDT")
        print(f"tick size     {loop.tick_size}")
        print(f"tape          /fapi/v1/aggTrades every {POLL_INTERVAL_SECONDS:g}s")
        print(f"calendar      {FEED_URL}")
        print("orders        none; this loop has no execution port to submit to")
        print("runs for      " + ("until stopped" if run_for is None else str(run_for)))
        print("stop with Ctrl-C")
        stop = asyncio.Event()
        try:
            # A termination signal becomes the stop both halves already watch,
            # so a supervisor ending the run gets the same wind-down as an
            # operator pressing Ctrl-C rather than a process killed mid-write.
            async with stop_on_signals(stop), stop_after(stop, run_for):
                # Together, because neither is useful alone: without the tape
                # every pass quietly produces nothing, and without the loop
                # the tape fills a table nobody reads.
                await run_together(
                    tape.run(stop=stop),
                    run_forever(
                        ports=loop.ports,
                        clock=SystemClock(),
                        interval=HLIT_TIMEFRAME,
                        stop=stop,
                    ),
                    # The pass renews the lease too, but on the evaluation's
                    # cadence, which has nothing to do with the lease's term.
                    heartbeat.run(stop=stop),
                    stop=stop,
                )
            print("stopped")
        except KeyboardInterrupt:
            # Reachable only in the window before the handlers are installed.
            print("stopped")
        finally:
            print(
                f"tape          {tape.trades} trades over {tape.polls} polls "
                f"({tape.failures} failures, {tape.deferred} not leader)"
            )
            print(
                f"calendar      {loop.events.fetches} fetches "
                f"({loop.events.failures} failures)"
            )
            print(
                f"lease         {heartbeat.renewals} renewals ({heartbeat.losses} lost)"
            )
            await loop.events.aclose()
            await loop.rest.aclose()
    finally:
        await engine.dispose()
    return 0


async def _report(alias: str) -> int:
    settings = Settings()
    engine = create_engine(settings)
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        try:
            resolved = await resolve_account(
                sessions,
                account_alias=alias,
                market=MARKET,
                instrument_code=BTCUSDT.code,
                exchange_code=BINANCE_USDM_EXCHANGE_CODE,
            )
        except StartupRefusedError as error:
            print(str(error), file=sys.stderr)
            return 1
    finally:
        await engine.dispose()

    print(f"account       {alias} ({resolved.account.broker_code})")
    print(f"environment   {resolved.account.environment}")
    print(f"market        {resolved.market.value}")
    print(f"instrument    {resolved.instrument_id}")
    print(f"policy        {resolved.policy_version_id}")
    print(f"manifest      {resolved.manifest_id}")
    print(f"binding       {resolved.binding_id}")

    missing = unsourced_inputs()
    if not missing:
        return 0
    # Named, not counted against the account. These are observations with no
    # producer, and none of them is consulted by a decision, so the resolution
    # above succeeded and this is what will be absent from it.
    print()
    print(f"{len(missing)} observations have no producer and will be absent:")
    for item in missing:
        print(f"  - {item.name}: {item.reason}")
    return 0


def main(argv: tuple[str, ...]) -> int:
    alias = _value(argv, "--account")
    if alias is None:
        print(USAGE, file=sys.stderr)
        return 2
    if "--check" in argv:
        return asyncio.run(_report(alias))
    if "--run" not in argv:
        print(USAGE, file=sys.stderr)
        return 2
    # Shadow is the only mode that exists, and naming it is deliberate: a
    # `--run` that quietly chose one would be a flag away from choosing
    # another.
    if "--shadow" not in argv:
        print(
            "only --shadow runs; Paper and LIVE are sections 11.7 and 11.8, "
            "behind two Shadow and two Paper sessions.",
            file=sys.stderr,
        )
        return 2
    leverage = _value(argv, "--leverage")
    if leverage is None or not leverage.isdigit() or int(leverage) <= 0:
        # Not on any position the venue reports, and it decides the size of an
        # order, so it is stated rather than guessed.
        print(
            "--leverage <n> is required and is not read from the venue",
            file=sys.stderr,
        )
        return 2
    run_for: timedelta | None = None
    requested = _value(argv, "--for")
    if requested is not None:
        try:
            run_for = parse_duration(requested)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
    return asyncio.run(_run_shadow(alias, int(leverage), run_for))


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
