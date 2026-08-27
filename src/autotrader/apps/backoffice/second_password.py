"""The second password, and what it is allowed to authorize.

Section 9 separates two kinds of control. Stopping is always available and
needs only a session and a form token. Starting — arming, clearing a halt — is
a dangerous action: it needs the password again, and the approval it produces
is bound so tightly that it cannot be spent on anything other than the exact
thing the operator was looking at when they typed it.

Nothing here stores or logs password material. What is stored is an Argon2id
verifier; what is logged is that a check happened and whether it passed.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.persistence.mysql.models.backoffice import (
    BackofficeSecondPasswordVersionRow,
)
from autotrader.security.second_password import (
    hash_second_password,
    verify_second_password,
)
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc

APPROVAL_PREFIX = "backoffice:approval:"
ATTEMPT_PREFIX = "backoffice:attempt:"
APPROVAL_LIFETIME = timedelta(seconds=60)
# Long enough that a run of failures is still throttled after the operator
# stops trying, short enough that a mistyped password is not a lockout.
ATTEMPT_WINDOW = timedelta(minutes=15)
MAX_ATTEMPTS = 5
_APPROVAL_ID_BYTES = 32


class SecondPasswordUnsetError(RuntimeError):
    """Raised when no second password has been established."""


class ApprovalRejectedError(RuntimeError):
    """Raised when an approval is missing, spent, or bound to something else."""


class TooManyAttemptsError(RuntimeError):
    """Raised when a session or address has failed too often too recently."""


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Exactly what the operator was looking at when they typed the password.

    The digest is over the state shown on the confirmation panel. If anything
    it named has changed by the time the approval is spent, the approval no
    longer matches and the action does not happen.
    """

    session_id: str
    operator_email: str
    action: str
    target_type: str
    target_key: str
    authority_digest: bytes

    def __post_init__(self) -> None:
        if len(self.authority_digest) != 32:
            raise ValueError("authority digest must be SHA-256 bytes")
        for name in ("session_id", "operator_email", "action", "target_type"):
            if not getattr(self, name):
                raise ValueError(f"{name} is required")

    def binding(self) -> str:
        """What the approval may be spent on, and nothing else."""
        return hashlib.sha256(
            json.dumps(
                {
                    "session_id": self.session_id,
                    "operator_email": self.operator_email,
                    "action": self.action,
                    "target_type": self.target_type,
                    "target_key": self.target_key,
                    "authority_digest": self.authority_digest.hex(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()


class ApprovalClient(Protocol):
    """The subset of redis-py the approval and its rate limit use."""

    def set(
        self, name: str, value: str, *, ex: int, nx: bool
    ) -> Awaitable[bool | None]: ...

    def get(self, name: str) -> Awaitable[str | bytes | None]: ...

    def getdel(self, name: str) -> Awaitable[str | bytes | None]: ...

    def incr(self, name: str) -> Awaitable[int]: ...

    def expire(self, name: str, time: int) -> Awaitable[bool]: ...

    def delete(self, *names: str) -> Awaitable[int]: ...


class MySqlSecondPasswords:
    """The active verifier, and how a new one replaces it."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def active(self) -> BackofficeSecondPasswordVersionRow:
        async with self._sessions() as session:
            row = await self._active_in(session)
            if row is None:
                raise SecondPasswordUnsetError(
                    "no second password has been established"
                )
            return row

    async def establish(self, password: str, *, now: datetime) -> int:
        """Set the password, retiring whatever it replaces.

        Both rows move in one transaction. A schema that allows only one
        active marker would otherwise reject the new row or, worse, leave
        none active at all.
        """
        moment = require_utc(now)
        verifier = hash_second_password(password)
        async with self._sessions() as session:
            current = await self._active_in(session, lock=True)
            version = 1 if current is None else current.version + 1
            if current is not None:
                current.retired_at = moment
                current.active_marker = None
                await session.flush()
            session.add(
                BackofficeSecondPasswordVersionRow(
                    id=new_uuid7(),
                    version=version,
                    verifier=verifier,
                    created_at=moment,
                    retired_at=None,
                    active_marker="ACTIVE",
                )
            )
            await session.commit()
        return version

    async def _active_in(
        self, session: AsyncSession, *, lock: bool = False
    ) -> BackofficeSecondPasswordVersionRow | None:
        statement = select(BackofficeSecondPasswordVersionRow).where(
            BackofficeSecondPasswordVersionRow.active_marker == "ACTIVE"
        )
        if lock:
            statement = statement.with_for_update()
        return await session.scalar(statement)


class ApprovalStore:
    """Approvals and failed attempts, both short lived, both in Redis."""

    def __init__(self, client: ApprovalClient) -> None:
        self._client = client

    async def issue(self, request: ApprovalRequest) -> str:
        approval_id = secrets.token_urlsafe(_APPROVAL_ID_BYTES)
        stored = await self._client.set(
            f"{APPROVAL_PREFIX}{approval_id}",
            request.binding(),
            ex=int(APPROVAL_LIFETIME.total_seconds()),
            nx=True,
        )
        if not stored:
            raise ApprovalRejectedError("approval identity is already taken")
        return approval_id

    async def consume(self, approval_id: str, request: ApprovalRequest) -> None:
        """Spend the approval, or refuse.

        Read and delete are one command. Two would let a second request slip
        between them and spend the same approval twice.
        """
        stored = await self._client.getdel(f"{APPROVAL_PREFIX}{approval_id}")
        if stored is None:
            raise ApprovalRejectedError("approval is missing or already spent")
        binding = stored.decode("utf-8") if isinstance(stored, bytes) else stored
        if not secrets.compare_digest(binding, request.binding()):
            # Spent regardless, because an approval offered for the wrong
            # thing has been handled and must not be offered again.
            raise ApprovalRejectedError("approval does not authorize this action")

    async def record_failure(self, *, session_id: str, source_ip: str | None) -> None:
        for key in _attempt_keys(session_id, source_ip):
            count = await self._client.incr(key)
            if count == 1:
                await self._client.expire(key, int(ATTEMPT_WINDOW.total_seconds()))

    async def clear_failures(self, *, session_id: str, source_ip: str | None) -> None:
        await self._client.delete(*_attempt_keys(session_id, source_ip))

    async def require_attempts_left(
        self, *, session_id: str, source_ip: str | None
    ) -> None:
        """A read, and only a read. Counting it would throttle an operator
        for being shown the panel."""
        for key in _attempt_keys(session_id, source_ip):
            raw = await self._client.get(key)
            if raw is not None and int(raw) >= MAX_ATTEMPTS:
                raise TooManyAttemptsError("too many recent failures")


def _attempt_keys(session_id: str, source_ip: str | None) -> tuple[str, ...]:
    """Counted against both, because either alone is easy to change."""
    keys = [f"{ATTEMPT_PREFIX}session:{session_id}"]
    if source_ip:
        keys.append(f"{ATTEMPT_PREFIX}ip:{source_ip}")
    return tuple(keys)


def check_password(verifier: BackofficeSecondPasswordVersionRow, password: str) -> bool:
    return verify_second_password(verifier.verifier, password)


def authority_digest(facts: Mapping[str, object]) -> bytes:
    """A digest of the exact facts shown on the confirmation panel."""
    return hashlib.sha256(
        json.dumps(dict(facts), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).digest()


__all__ = (
    "APPROVAL_LIFETIME",
    "APPROVAL_PREFIX",
    "ATTEMPT_PREFIX",
    "ATTEMPT_WINDOW",
    "MAX_ATTEMPTS",
    "ApprovalClient",
    "ApprovalRejectedError",
    "ApprovalRequest",
    "ApprovalStore",
    "MySqlSecondPasswords",
    "SecondPasswordUnsetError",
    "TooManyAttemptsError",
    "authority_digest",
    "check_password",
)
