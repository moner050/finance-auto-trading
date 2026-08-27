"""Establish the second password, locally and without echo.

The password is never an argument. A command line lands in shell history, in
the process table, and in whatever collects either, and none of those are
places a credential can be taken back out of.

Run it where the database is reachable:

    python -m autotrader.apps.backoffice.bootstrap
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.apps.backoffice.second_password import MySqlSecondPasswords
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine

MINIMUM_LENGTH = 12
PROMPT = "second password: "
CONFIRM_PROMPT = "again: "


class BootstrapRefusedError(RuntimeError):
    """Raised when the entered password cannot be established."""


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


def read_password(prompt: Callable[[str], str] = getpass.getpass) -> str:
    entered = require_acceptable(prompt(PROMPT))
    if entered != prompt(CONFIRM_PROMPT):
        raise BootstrapRefusedError("the two entries did not match")
    return entered


async def establish(password: str, *, settings: Settings | None = None) -> int:
    resolved = settings or Settings()
    engine = create_engine(resolved)
    try:
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        return await MySqlSecondPasswords(sessions).establish(
            password, now=datetime.now(UTC)
        )
    finally:
        await engine.dispose()


def main(argv: tuple[str, ...]) -> int:
    if argv:
        # Refusing the argument is the point, so it is refused loudly rather
        # than ignored.
        print("this command takes no arguments", file=sys.stderr)
        return 2
    try:
        password = read_password()
    except BootstrapRefusedError as error:
        print(str(error), file=sys.stderr)
        return 1
    version = asyncio.run(establish(password))
    # The version, and nothing about the password itself.
    print(f"second password version {version} is active")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
