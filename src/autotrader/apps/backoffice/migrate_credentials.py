"""Move a provider's credentials out of the .env file and into the database.

The dotenv resolver already knows how to read them and what shape they are, so
this reads through it rather than parsing the file again. Reading twice is how
the two copies end up disagreeing about which key belongs to which
environment.

It never deletes anything. Removing the values from .env is the operator's
step, taken once they have seen the migration work, because a tool that
deletes the only copy of a credential the moment before something goes wrong
is a tool nobody should run.

    python -m autotrader.apps.backoffice.migrate_credentials KIS PAPER
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from autotrader.apps.backoffice.credentials import store_set
from autotrader.apps.backoffice.provider_secrets import (
    BINANCE,
    BINANCE_LIVE_REFERENCE,
    KIS,
    LIVE,
    PAPER,
    TOSS,
    ProviderField,
    fields_for,
)
from autotrader.config.account_secrets import (
    AccountSecretResolutionError,
    DotenvAccountSecretResolver,
)
from autotrader.config.settings import Settings

USAGE = (
    "usage: python -m autotrader.apps.backoffice.migrate_credentials "
    "<KIS|TOSS|BINANCE> <LIVE|PAPER> [env-file]"
)
DEFAULT_ENV_FILE = Path(".env")

_DOTENV_KIS = {LIVE: "secret://dotenv/kis/real", PAPER: "secret://dotenv/kis/paper"}
_DOTENV_TOSS = "secret://dotenv/toss/live"
_DOTENV_BINANCE = "secret://dotenv/binance/usdm/live"


class MigrationRefusedError(RuntimeError):
    """Raised when the values in the file cannot be migrated as asked."""


def read_from_dotenv(provider: str, environment: str, env_file: Path) -> dict[str, str]:
    """The values as the dotenv resolver understands them."""
    resolver = DotenvAccountSecretResolver(env_file)
    try:
        if provider == KIS:
            secret = resolver.resolve_kis(_DOTENV_KIS[environment])
            return {
                "app-key": secret.app_key.get_secret_value(),
                "app-secret": secret.app_secret.get_secret_value(),
                "account-number": secret.account_number.get_secret_value(),
                "product-code": secret.product_code,
            }
        if provider == TOSS:
            if environment != LIVE:
                raise MigrationRefusedError("Toss credentials are LIVE only")
            toss = resolver.resolve_toss(_DOTENV_TOSS)
            return {
                "client-id": toss.client_id.get_secret_value(),
                "client-secret": toss.client_secret.get_secret_value(),
            }
        if environment != LIVE:
            raise MigrationRefusedError("Binance credentials are LIVE only")
        binance = resolver.resolve_binance_usdm(_DOTENV_BINANCE)
        return {
            "api-key": binance.api_key.get_secret_value(),
            "secret-key": binance.secret_key.get_secret_value(),
        }
    except AccountSecretResolutionError as error:
        # The resolver deliberately says nothing about which value was
        # missing, so neither does this.
        raise MigrationRefusedError(
            f"the file does not hold a complete {provider} {environment} set"
        ) from error


async def migrate(
    provider: str,
    environment: str,
    env_file: Path,
    *,
    settings: Settings | None = None,
) -> tuple[ProviderField, ...]:
    values = read_from_dotenv(provider, environment, env_file)
    fields = fields_for(provider, environment)
    await store_set(fields, values, settings=settings)
    return fields


def main(argv: tuple[str, ...]) -> int:
    if (
        not 2 <= len(argv) <= 3
        or argv[0] not in (KIS, TOSS, BINANCE)
        or argv[1] not in (LIVE, PAPER)
    ):
        print(USAGE, file=sys.stderr)
        return 2
    provider, environment = argv[0], argv[1]
    env_file = Path(argv[2]) if len(argv) == 3 else DEFAULT_ENV_FILE
    try:
        fields = asyncio.run(migrate(provider, environment, env_file))
    except (MigrationRefusedError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"migrated {len(fields)} values for {provider} {environment}")
    for field in fields:
        print(f"  {field.reference}")
    print("the file still holds them; remove them once this has been verified")
    return 0


__all__ = (
    "BINANCE_LIVE_REFERENCE",
    "DEFAULT_ENV_FILE",
    "MigrationRefusedError",
    "main",
    "migrate",
    "read_from_dotenv",
)


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
