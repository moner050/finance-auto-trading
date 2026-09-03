"""The command ledger: who asked for what, and what came of it.

Section 13 asks for a durable record of every command, and the table was built
for it. Its status column is the part that shapes this file. IN_PROGRESS only
means anything if it is committed before the work starts, in its own
transaction — otherwise a rollback erases the evidence that anything was
attempted. So a command is opened, then done, then closed:

- opening commits IN_PROGRESS on its own;
- the work and the SUCCEEDED close are one transaction, so a record that
  cannot be written takes the change down with it; and
- a failure is closed separately, best effort, because a command that failed
  has already not happened and losing the note must not make it happen.

A row left at IN_PROGRESS is therefore a crash, and reads as one.

This ledger answers "what was asked for and did it work". The control's own
before-and-after history stays in ops_audit_log, which answers a different
question and is written in the same transaction, so the two cannot disagree.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.persistence.mysql.models.backoffice import BackofficeCommandRow
from autotrader.shared.time import require_utc

IN_PROGRESS = "IN_PROGRESS"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"


class SourceAddressUnknownError(RuntimeError):
    """Raised when a command arrives with no peer address to record."""


class LedgerConflictError(RuntimeError):
    """Raised when a command id has already been opened."""


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """What is written when a command opens."""

    id: UUID
    actor_email: str
    source_ip: str
    action: str
    target_type: str
    target_key: str
    payload: Mapping[str, object]
    expected_digest: bytes | None
    started_at: datetime

    def __post_init__(self) -> None:
        if not self.source_ip.strip():
            # The schema requires it, and so does the audit contract. A
            # placeholder would be a recorded fact that is not true.
            raise SourceAddressUnknownError("a command must record where it came from")
        if self.expected_digest is not None and len(self.expected_digest) != 32:
            raise ValueError("expected_digest must be SHA-256 bytes")
        object.__setattr__(self, "started_at", require_utc(self.started_at))

    def payload_digest(self) -> bytes:
        return digest_of(self.payload)


def digest_of(payload: Mapping[str, object]) -> bytes:
    return hashlib.sha256(
        json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).digest()


class MySqlCommandLedger:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def open(self, entry: LedgerEntry) -> None:
        """Record the attempt, and commit it before anything is attempted."""
        async with self._sessions() as session:
            if await session.get(BackofficeCommandRow, entry.id) is not None:
                raise LedgerConflictError("this command has already been opened")
            session.add(
                BackofficeCommandRow(
                    id=entry.id,
                    actor_email=entry.actor_email,
                    source_ip=entry.source_ip,
                    action=entry.action,
                    target_type=entry.target_type,
                    target_key=entry.target_key,
                    payload_digest=entry.payload_digest(),
                    expected_digest=entry.expected_digest,
                    status=IN_PROGRESS,
                    result_code=None,
                    result_digest=None,
                    started_at=entry.started_at,
                    completed_at=None,
                )
            )
            await session.commit()

    async def succeed(
        self,
        session: AsyncSession,
        *,
        command_id: UUID,
        result_code: str,
        result: Mapping[str, object],
        completed_at: datetime,
    ) -> None:
        """Close the command in the caller's transaction.

        Deliberately not given its own session: section 13 requires the
        committed state and its success record to be one transaction, so a
        record that cannot be written rolls the change back with it.
        """
        row = await self._locked(session, command_id)
        row.status = SUCCEEDED
        row.result_code = result_code
        row.result_digest = digest_of(result)
        row.completed_at = require_utc(completed_at)
        await session.flush()

    async def fail(
        self, *, command_id: UUID, result_code: str, completed_at: datetime
    ) -> None:
        """Close a command that did not happen, in its own transaction.

        Best effort on purpose. The change has already not been made, and a
        ledger write that fails here must not be able to undo that.
        """
        async with self._sessions() as session:
            row = await self._locked(session, command_id)
            if row.status != IN_PROGRESS:
                return
            row.status = FAILED
            row.result_code = result_code
            # The schema wants a digest either way, and the failure code is
            # the only outcome there is.
            row.result_digest = digest_of({"failure": result_code})
            row.completed_at = require_utc(completed_at)
            await session.commit()

    async def _locked(
        self, session: AsyncSession, command_id: UUID
    ) -> BackofficeCommandRow:
        row = await session.scalar(
            select(BackofficeCommandRow)
            .where(BackofficeCommandRow.id == command_id)
            .with_for_update()
        )
        if row is None:
            raise LedgerConflictError("this command was never opened")
        return row


__all__ = (
    "FAILED",
    "IN_PROGRESS",
    "SUCCEEDED",
    "LedgerConflictError",
    "LedgerEntry",
    "MySqlCommandLedger",
    "SourceAddressUnknownError",
    "digest_of",
)
