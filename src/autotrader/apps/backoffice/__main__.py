"""Run the backoffice.

The account it reports on is named on the command line rather than guessed,
because a dashboard that silently picks an account is a dashboard that can
show the wrong one.
"""

from __future__ import annotations

import asyncio
import sys
from uuid import UUID

import uvicorn
from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.apps.backoffice.composition import build_backoffice
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.shared.origins import require_public_origin

USAGE = "usage: python -m autotrader.apps.backoffice <account-uuid>"


def main(argv: tuple[str, ...]) -> int:
    if len(argv) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    settings = Settings()
    require_public_origin(
        str(settings.backoffice_public_url), name="BACKOFFICE_PUBLIC_URL"
    )
    # Built before the server starts, because reading the configuration
    # touches the database and a server that binds first would be listening
    # while it discovered it could not authenticate.
    app = asyncio.run(
        build_backoffice(
            settings=settings,
            sessions=async_sessionmaker(
                bind=create_engine(settings), expire_on_commit=False
            ),
            account_id=UUID(argv[0]),
        )
    )
    # The public URL is validated because it has to match what is registered
    # with the identity provider. It is not where the process listens.
    uvicorn.run(
        app,
        host=settings.backoffice_bind_host,
        port=settings.backoffice_bind_port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
