"""Run the backoffice.

The account it reports on is named on the command line rather than guessed,
because a dashboard that silently picks an account is a dashboard that can
show the wrong one.
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import urlsplit
from uuid import UUID

import uvicorn
from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.apps.backoffice.composition import build_backoffice
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.shared.origins import is_loopback, require_public_origin

USAGE = "usage: python -m autotrader.apps.backoffice <account-uuid>"


def require_reachable_public_url(settings: Settings) -> None:
    """Refuse a public URL the browser will not find this process at.

    Behind a reverse proxy the two ports differ on purpose - Caddy publishes
    443 and the app binds 8000 on a network only the proxy can reach - so this
    only applies when the public URL is loopback, which is what says there is
    no proxy in front.

    In that case the browser connects to the process directly, and the two
    ports have to be the same one. They were not: the public URL named 6086
    and the bind port defaulted to 8000, so the identity provider would have
    sent the operator back to a port nothing was listening on. Everything up
    to that point looks correct, which is why it is worth refusing here.
    """
    public_url = str(settings.backoffice_public_url)
    if not is_loopback(public_url):
        return
    published = urlsplit(public_url).port or (
        443 if public_url.startswith("https://") else 80
    )
    if published != settings.backoffice_bind_port:
        raise SystemExit(
            f"BACKOFFICE_PUBLIC_URL points at port {published} on loopback but "
            f"BACKOFFICE_BIND_PORT is {settings.backoffice_bind_port}; with no "
            "proxy in front the callback would reach nothing"
        )


def main(argv: tuple[str, ...]) -> int:
    if len(argv) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    settings = Settings()
    require_public_origin(
        str(settings.backoffice_public_url), name="BACKOFFICE_PUBLIC_URL"
    )
    require_reachable_public_url(settings)
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
