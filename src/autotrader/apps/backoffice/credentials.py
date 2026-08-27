"""Put a provider's credentials into the database, without echoing them.

A whole set at a time, in one transaction. Storing them one by one would leave
an account that has three of its four values, which resolves as unconfigured
and reads as a missing account rather than a half-finished job.

Rotation goes through the same path: a new version, a moved activation, the
old version still on record. Nothing is edited in place.

    python -m autotrader.apps.backoffice.credentials KIS PAPER
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.apps.backoffice.bootstrap import master_key_ring
from autotrader.apps.backoffice.provider_secrets import (
    BINANCE,
    KIS,
    LIVE,
    PAPER,
    TOSS,
    ProviderField,
    fields_for,
)
from autotrader.apps.backoffice.secrets import MySqlSecretStore
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine

USAGE = (
    "usage: python -m autotrader.apps.backoffice.credentials "
    "<KIS|TOSS|BINANCE> <LIVE|PAPER>"
)
PROVIDERS = (KIS, TOSS, BINANCE)
ENVIRONMENTS = (LIVE, PAPER)


class CredentialsRefusedError(RuntimeError):
    """Raised when a credential set cannot be stored as asked."""


def read_values(
    fields: tuple[ProviderField, ...],
    prompt: Callable[[str], str] = getpass.getpass,
) -> dict[str, str]:
    """Collect every field before storing any of them."""
    values: dict[str, str] = {}
    for field in fields:
        entered = prompt(f"{field.logical_name}: ").strip()
        if not entered:
            raise CredentialsRefusedError(f"{field.logical_name} is required")
        if "\n" in entered or "\r" in entered:
            # The credential types refuse these, and finding out at signing
            # time would read as a rejected request.
            raise CredentialsRefusedError(f"{field.logical_name} must be a single line")
        values[field.field] = entered
    return values


async def store_set(
    fields: tuple[ProviderField, ...],
    values: Mapping[str, str],
    *,
    settings: Settings | None = None,
) -> None:
    resolved = settings or Settings()
    engine = create_engine(resolved)
    try:
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        store = MySqlSecretStore(sessions, master_key_ring(resolved))
        now = datetime.now(UTC)
        async with sessions() as session:
            for field in fields:
                await store.store_in(
                    session,
                    logical_name=field.logical_name,
                    scope=field.scope,
                    plaintext=values[field.field],
                    now=now,
                )
            # One transaction, so a set is complete or absent.
            await session.commit()
    finally:
        await engine.dispose()


def main(argv: tuple[str, ...]) -> int:
    if len(argv) != 2 or argv[0] not in PROVIDERS or argv[1] not in ENVIRONMENTS:
        print(USAGE, file=sys.stderr)
        return 2
    provider, environment = argv
    try:
        fields = fields_for(provider, environment)
        values = read_values(fields)
        asyncio.run(store_set(fields, values))
    except (CredentialsRefusedError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    # The names that were stored, and nothing that was stored under them.
    print(f"stored {len(fields)} values for {provider} {environment}")
    for field in fields:
        print(f"  {field.reference}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
