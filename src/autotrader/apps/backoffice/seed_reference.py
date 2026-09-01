"""Put the reference rows into a real database.

The seed existed and had no way to reach a database anybody uses. It is
applied by a paper-trading harness and by tests, both of which build their own
schema, so a production database migrated by Alembic came up with the tables
and none of the rows: no data sources, no markets, no exchanges, no brokers.

That is not visible until something asks for one. The accounts screen offers a
broker to pick, reads `exec_broker`, finds it empty, and the first account
cannot be created - on a database where every migration reported success.

Idempotent, because reference data is a statement about what has been
implemented rather than an event. `ensure_exact_seed_row` refuses a row that
exists with different values instead of overwriting it: a seed that quietly
corrected the database would hide a schema and a build that disagree.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.apps.trader.binance_paper import BTCUSDT
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.repositories.core import CoreInstrumentRegistry
from autotrader.persistence.mysql.seeds.core import seed_core_reference_session
from autotrader.persistence.mysql.seeds.david_v6 import register_david_v6_build

USAGE = "usage: python -m autotrader.apps.backoffice.seed_reference"


async def apply_reference_seed(settings: Settings) -> None:
    engine = create_engine(settings)
    try:
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with sessions() as session:
            await seed_core_reference_session(session)
            # The one instrument this system trades. `--check` names it as
            # BINANCE_USDM:BTCUSDT and nothing outside the paper harness ever
            # registered it.
            await CoreInstrumentRegistry(session).register(BTCUSDT)
            # The build a decision is recorded under. Derived from the code,
            # not chosen: source, design and configuration hashes all come
            # from the manifest module and the repository re-checks each one.
            manifest_id = await register_david_v6_build(session, now=datetime.now(UTC))
            # One transaction: a half-seeded reference set is a database that
            # looks prepared and is not.
            await session.commit()
        print(f"strategy manifest {manifest_id}")
    finally:
        await engine.dispose()


def main(argv: tuple[str, ...]) -> int:
    if argv:
        print(USAGE, file=sys.stderr)
        return 2
    settings = Settings()
    asyncio.run(apply_reference_seed(settings))
    print("reference rows applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
