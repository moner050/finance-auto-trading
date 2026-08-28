"""Start the trader, or say exactly why it cannot start.

    python -m autotrader.apps.trader --account <alias> --check

`--check` resolves everything and reports, without connecting to a venue or
placing anything. It is the useful mode today: six of the loop's inputs have no
producer anywhere in the system, so the honest output of this program is the
list of them rather than a running loop.

Only the Binance USD-M paper loop is wired. It is the one composition that is
complete end to end; an entry point that offered all three markets would refuse
for two of them and teach nobody anything by doing so.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.apps.trader.binance_paper import BTCUSDT
from autotrader.apps.trader.startup import (
    StartupRefusedError,
    resolve_account,
    unsourced_inputs,
)
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.seeds.core import BINANCE_USDM_EXCHANGE_CODE
from autotrader.strategies.david_v6.models import V6Market

USAGE = "usage: python -m autotrader.apps.trader --account <alias> [--check]"
MARKET = V6Market.BINANCE_USDM


def _account_alias(argv: tuple[str, ...]) -> str | None:
    for index, item in enumerate(argv):
        if item == "--account" and index + 1 < len(argv):
            return argv[index + 1]
    return None


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
    # Reported on stderr and with a non-zero exit, because this is the reason
    # the loop is not running, not a footnote to a successful resolution.
    print("", file=sys.stderr)
    print(
        f"the loop cannot start; {len(missing)} inputs have no producer:",
        file=sys.stderr,
    )
    for item in missing:
        print(f"  - {item.name}: {item.reason}", file=sys.stderr)
    return 1


def main(argv: tuple[str, ...]) -> int:
    alias = _account_alias(argv)
    if alias is None:
        print(USAGE, file=sys.stderr)
        return 2
    if "--check" not in argv:
        # There is no running mode yet, and offering one that fell over after
        # connecting to a venue would be worse than not offering it.
        print(
            "only --check is supported; the loop has inputs with no producer.\n"
            "Run with --check to see which.",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(_report(alias))


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
