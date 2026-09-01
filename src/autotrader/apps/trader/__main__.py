"""Start the trader in Shadow, or say exactly why it cannot start.

    python -m autotrader.apps.trader --account <alias> --check
    python -m autotrader.apps.trader --account <alias> --run --shadow --leverage <n>

`--check` resolves everything and reports without connecting to a venue.

`--run` needs `--shadow` spelled out. It is the only mode that exists, and a
`--run` that quietly chose one would be a flag away from choosing another;
Paper and LIVE are sections 11.7 and 11.8, behind two Shadow and two Paper
sessions. The Shadow loop evaluates real bars and records real decisions
through an execution port with no broker behind it.

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
    "--shadow --leverage <n>",
)
USAGE = "\n".join(_USAGE_LINES)
MARKET = V6Market.BINANCE_USDM


def _value(argv: tuple[str, ...], flag: str) -> str | None:
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            return argv[index + 1]
    return None


async def _run_shadow(alias: str, leverage: int) -> int:
    """Evaluate real bars and record real decisions, placing nothing."""
    from autotrader.apps.trader.composition import bound_policy
    from autotrader.apps.trader.loop import SystemClock, run_forever
    from autotrader.apps.trader.run_shadow import ShadowStartupError, build_shadow_loop
    from autotrader.apps.trader.shadow import SHADOW

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

        print(f"mode          {SHADOW}")
        print(f"account       {alias} ({resolved.account.environment})")
        print(f"equity        {loop.equity} USDT")
        print(f"tick size     {loop.tick_size}")
        print("orders        none; this loop has no execution port to submit to")
        print("stop with Ctrl-C")
        try:
            await run_forever(
                ports=loop.ports,
                clock=SystemClock(),
                interval=HLIT_TIMEFRAME,
                stop=asyncio.Event(),
            )
        except KeyboardInterrupt:
            print("stopped")
        finally:
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
    return asyncio.run(_run_shadow(alias, int(leverage)))


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
