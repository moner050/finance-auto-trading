"""Bring a live database up to the models, and say what it cannot fix.

This project keeps one consolidated migration, regenerated from the ORM
metadata and checked by a gate. That works because every test database is
built from scratch, and it silently does not work for a database that already
exists: production was stamped `0001_initial` long ago, the revision was
rewritten underneath it several times since, and `alembic upgrade head` has
nothing to do because the stamp already matches. Four tables were missing
before anybody looked.

So the drift is reported rather than assumed away, and the additive part of it
is applied. What this deliberately does not do is guess: a table that exists
with the wrong columns, an index that was renamed, a constraint that was
tightened - none of that is repaired here, because repairing it means deciding
what to do with the rows already in it, and that is a migration somebody
writes and reads.

Missing tables are the safe case and the common one. Creating one adds
nothing to any existing row.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models import metadata

USAGE = "usage: python -m autotrader.apps.backoffice.apply_schema [--apply]"


def _drift(connection: Connection) -> tuple[list[str], list[str]]:
    """Tables the models have and the database does not, and column drift."""
    inspector = inspect(connection)
    live = set(inspector.get_table_names())
    missing_tables = sorted(set(metadata.tables) - live)
    missing_columns: list[str] = []
    for name, table in sorted(metadata.tables.items()):
        if name not in live:
            continue
        stored = {column["name"] for column in inspector.get_columns(name)}
        for column in table.columns:
            if column.name not in stored:
                missing_columns.append(f"{name}.{column.name}")
    return missing_tables, missing_columns


async def report(settings: Settings, *, apply: bool) -> int:
    engine = create_engine(settings)
    try:
        async with engine.begin() as connection:
            missing_tables, missing_columns = await connection.run_sync(_drift)
            if missing_tables and apply:
                await connection.run_sync(
                    metadata.create_all,
                    tables=[metadata.tables[name] for name in missing_tables],
                    checkfirst=True,
                )
    finally:
        await engine.dispose()

    if not missing_tables and not missing_columns:
        print("the database matches the models")
        return 0

    for name in missing_tables:
        print(f"{'created' if apply else 'missing'} table {name}")
    for column in missing_columns:
        # Named and not touched. Adding one means deciding what every existing
        # row holds in it, which is a migration somebody writes.
        print(f"missing column {column} (not applied; write a migration)")

    if missing_columns:
        return 1
    return 0 if apply else 1


def main(argv: tuple[str, ...]) -> int:
    if set(argv) - {"--apply"}:
        print(USAGE, file=sys.stderr)
        return 2
    return asyncio.run(report(Settings(), apply="--apply" in argv))


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
