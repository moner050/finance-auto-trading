"""Establish the backoffice, locally and without echo.

Nothing here is an argument. A command line lands in shell history, in the
process table, and in whatever collects either, and none of those are places a
credential can be taken back out of.

Four things become true together or not at all: the encrypted OAuth client id,
the encrypted client secret, the Argon2id verifier, and the authority row that
says the backoffice is bootstrapped. Half of that standing would be a system
that believes it is configured and cannot sign anybody in.

Section 8.4: this succeeds only when no authority exists. Rotation afterwards
is done from the authenticated GUI, not by running this again.

    python -m autotrader.apps.backoffice.bootstrap
"""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import sys
from base64 import b64decode
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.second_password import MySqlSecondPasswords
from autotrader.apps.backoffice.secrets import (
    OAUTH,
    MySqlSecretStore,
    SecretScope,
)
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.backoffice import (
    BackofficeBootstrapAuthorityRow,
)
from autotrader.security.secret_crypto import MasterKeyRing
from autotrader.shared.ids import new_uuid7

MINIMUM_LENGTH = 12
GOOGLE_CLIENT_ID = "google-oauth-client-id"
GOOGLE_CLIENT_SECRET = "google-oauth-client-secret"
GOOGLE_SCOPE = SecretScope(category=OAUTH, provider_code="GOOGLE", environment=None)
SCOPE_KEY = "PRIMARY"


class BootstrapRefusedError(RuntimeError):
    """Raised when the backoffice cannot be established as asked."""


@dataclass(frozen=True, slots=True)
class BootstrapInput:
    """Everything the command collects, held only long enough to store it."""

    client_id: str
    client_secret: str
    second_password: str


def require_acceptable(password: str) -> str:
    """What this refuses, and nothing more.

    A length floor is a real constraint. Composition rules — a digit, a
    symbol, a capital — push people towards one predictable password rather
    than a long one, so they are not here.
    """
    if type(password) is not str or len(password) < MINIMUM_LENGTH:
        raise BootstrapRefusedError(
            f"the second password must be at least {MINIMUM_LENGTH} characters"
        )
    if password != password.strip():
        # Surrounding space is invisible on retyping and would lock the
        # operator out of their own halt-clearing.
        raise BootstrapRefusedError("the second password must not be padded")
    return password


def read_input(prompt: Callable[[str], str] = getpass.getpass) -> BootstrapInput:
    client_id = _required(prompt("google oauth client id: "), "client id")
    client_secret = _required(prompt("google oauth client secret: "), "client secret")
    password = require_acceptable(prompt("second password: "))
    if password != prompt("second password again: "):
        raise BootstrapRefusedError("the two entries did not match")
    return BootstrapInput(
        client_id=client_id, client_secret=client_secret, second_password=password
    )


def master_key_ring(settings: Settings) -> MasterKeyRing:
    """The key that opens the secrets, which lives only in the server's .env."""
    key = settings.backoffice_master_key
    version = settings.backoffice_master_key_version
    if key is None or version is None:
        raise BootstrapRefusedError(
            "BACKOFFICE_MASTER_KEY and BACKOFFICE_MASTER_KEY_VERSION are required"
        )
    previous = settings.backoffice_previous_master_key
    previous_version = settings.backoffice_previous_master_key_version
    return MasterKeyRing(
        current_key=b64decode(key.get_secret_value(), validate=True),
        current_version=version,
        previous_key=(
            None
            if previous is None
            else b64decode(previous.get_secret_value(), validate=True)
        ),
        previous_version=previous_version,
    )


async def already_bootstrapped(session: AsyncSession) -> bool:
    return (
        await session.scalar(
            select(BackofficeBootstrapAuthorityRow.id).where(
                BackofficeBootstrapAuthorityRow.scope_key == SCOPE_KEY
            )
        )
    ) is not None


async def establish(
    collected: BootstrapInput, *, settings: Settings | None = None
) -> None:
    resolved = settings or Settings()
    keys = master_key_ring(resolved)
    engine = create_engine(resolved)
    try:
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        store = MySqlSecretStore(sessions, keys)
        passwords = MySqlSecondPasswords(sessions)
        async with sessions() as session:
            if await already_bootstrapped(session):
                # Not an overwrite. Rotation happens from the authenticated
                # GUI, where it is audited and needs the current password.
                raise BootstrapRefusedError(
                    "this backoffice is already bootstrapped; rotate from the GUI"
                )
            now = datetime.now(UTC)
            client_id_secret = await store.store_in(
                session,
                logical_name=GOOGLE_CLIENT_ID,
                scope=GOOGLE_SCOPE,
                plaintext=collected.client_id,
                now=now,
            )
            client_secret_secret = await store.store_in(
                session,
                logical_name=GOOGLE_CLIENT_SECRET,
                scope=GOOGLE_SCOPE,
                plaintext=collected.client_secret,
                now=now,
            )
            password_row = await passwords.establish_in(
                session, collected.second_password, now=now
            )
            session.add(
                BackofficeBootstrapAuthorityRow(
                    id=new_uuid7(),
                    scope_key=SCOPE_KEY,
                    oauth_client_id_secret_id=client_id_secret,
                    oauth_client_secret_secret_id=client_secret_secret,
                    second_password_version_id=password_row.id,
                    # Names what was bootstrapped, never what was in it.
                    bootstrap_digest=hashlib.sha256(
                        b"|".join(
                            (
                                client_id_secret.bytes,
                                client_secret_secret.bytes,
                                password_row.id.bytes,
                            )
                        )
                    ).digest(),
                    completed_at=now,
                )
            )
            await session.commit()
    finally:
        await engine.dispose()


def main(argv: tuple[str, ...]) -> int:
    if argv:
        # Refusing the argument is the point, so it is refused loudly rather
        # than ignored.
        print("this command takes no arguments", file=sys.stderr)
        return 2
    try:
        collected = read_input()
        asyncio.run(establish(collected))
    except BootstrapRefusedError as error:
        print(str(error), file=sys.stderr)
        return 1
    # What was established, and nothing that was established with.
    print("backoffice bootstrapped: oauth client stored, second password active")
    return 0


def _required(value: str, name: str) -> str:
    if not value.strip():
        raise BootstrapRefusedError(f"the {name} is required")
    return value.strip()


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
