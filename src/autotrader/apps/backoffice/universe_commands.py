"""Uploading a universe manifest, and putting one in force.

Two commands, and only one of them is dangerous. Staging stores a list that
nothing reads, so it needs the session and the CSRF token and no more; section
9 asks for the second password on universe *activation*, which is the moment
the strategy's filter changes underneath a running loop.

The upload validates before it stores. A digest the operator computed from
their copy of the file has to match the digest we compute from ours, because
that agreement is the only evidence the two are the same file. Provenance is
recorded but not digested - see the manifest module for why.

There is no command here that adds or removes a symbol. Section 11.5 rules
that out, and leaving the capability out of the code is how it stays ruled
out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.application.universe_manifest import (
    UniverseManifest,
    UniverseManifestError,
    compare,
    parse_manifest,
    verify_digest,
)
from autotrader.apps.backoffice.auth import Operator
from autotrader.apps.backoffice.ledger import LedgerEntry, MySqlCommandLedger
from autotrader.apps.backoffice.second_password import (
    ApprovalRequest,
    ApprovalStore,
    authority_digest,
)
from autotrader.persistence.mysql.repositories.universe import (
    StoredSnapshot,
    UniverseAuthorities,
    UniverseAuthorityError,
)
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc

TARGET_TYPE = "UNIVERSE_SNAPSHOT"
STAGE = "STAGE_UNIVERSE_SNAPSHOT"
ACTIVATE = "ACTIVATE_UNIVERSE_SNAPSHOT"

# Big enough for an S&P 100 or KOSPI 200 list with sectors and room to spare,
# small enough that a mistyped path does not become a database write.
MAXIMUM_UPLOAD_BYTES = 1_000_000


class UniverseUploadRefused(ValueError):
    """Raised when an upload is not an authority we will store."""


@dataclass(frozen=True, slots=True)
class ActivationFacts:
    """What the panel shows before the password is typed.

    The counts and the difference are here rather than only on the page,
    because the approval is bound to this digest: an operator who approves a
    change of three symbols cannot have that approval spend on a change of
    thirty.
    """

    snapshot_id: UUID
    universe_code: str
    effective_date: str
    member_count: int
    digest: str
    current_effective_date: str | None
    current_member_count: int | None
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]

    def as_details(self) -> dict[str, object]:
        return {
            "snapshot_id": str(self.snapshot_id),
            "universe_code": self.universe_code,
            "effective_date": self.effective_date,
            "member_count": self.member_count,
            "digest": self.digest,
            "current_effective_date": self.current_effective_date,
            "current_member_count": self.current_member_count,
            "added": list(self.added),
            "removed": list(self.removed),
            "changed": list(self.changed),
        }

    def digest_bytes(self) -> bytes:
        return authority_digest(self.as_details())


@dataclass(frozen=True, slots=True)
class ActivationCommand:
    id: UUID
    snapshot_id: UUID
    operator: Operator
    source_ip: str
    correlation_id: str
    approval_id: str
    requested_at: datetime

    def __post_init__(self) -> None:
        if self.id.version != 7:
            raise ValueError("command id must be UUIDv7")
        if not self.approval_id:
            raise ValueError("approval_id is required")
        object.__setattr__(self, "requested_at", require_utc(self.requested_at))


def new_activation_command(
    *,
    snapshot_id: UUID,
    operator: Operator,
    source_ip: str,
    correlation_id: str,
    approval_id: str,
    requested_at: datetime,
) -> ActivationCommand:
    return ActivationCommand(
        id=new_uuid7(),
        snapshot_id=snapshot_id,
        operator=operator,
        source_ip=source_ip,
        correlation_id=correlation_id,
        approval_id=approval_id,
        requested_at=requested_at,
    )


def approval_for(
    *, session_id: str, operator: Operator, facts: ActivationFacts
) -> ApprovalRequest:
    return ApprovalRequest(
        session_id=session_id,
        operator_email=operator.email,
        action=ACTIVATE,
        target_type=TARGET_TYPE,
        target_key=f"{facts.universe_code}:{facts.effective_date}",
        authority_digest=facts.digest_bytes(),
    )


def read_upload(document: bytes, *, claimed_digest: str | None) -> UniverseManifest:
    """Parse an uploaded file, and refuse it before anything is stored.

    A claimed digest is optional because not every published list arrives with
    one. When it is given it is checked, and a mismatch is refused rather than
    stored with a note: a snapshot whose provenance is in doubt is not an
    authority.
    """
    if type(document) is not bytes:
        raise UniverseUploadRefused("업로드된 파일을 읽지 못했습니다.")
    if not document:
        raise UniverseUploadRefused("빈 파일은 권위가 아닙니다.")
    if len(document) > MAXIMUM_UPLOAD_BYTES:
        raise UniverseUploadRefused("파일이 너무 큽니다.")
    try:
        manifest = parse_manifest(document)
    except UniverseManifestError as error:
        raise UniverseUploadRefused(str(error)) from error
    if claimed_digest:
        try:
            claimed = bytes.fromhex(claimed_digest.strip())
        except ValueError as error:
            raise UniverseUploadRefused(
                "다이제스트는 64자리 16진수여야 합니다."
            ) from error
        try:
            verify_digest(manifest, claimed=claimed)
        except UniverseManifestError as error:
            raise UniverseUploadRefused(str(error)) from error
    return manifest


class MySqlUniverseCommands:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        approvals: ApprovalStore,
        ledger: MySqlCommandLedger | None = None,
    ) -> None:
        self._sessions = sessions
        self._approvals = approvals
        self._ledger = ledger or MySqlCommandLedger(sessions)

    async def stage(
        self,
        manifest: UniverseManifest,
        *,
        operator: Operator,
        source_ip: str,
        now: datetime,
    ) -> StoredSnapshot:
        """Store a manifest where it can be compared but not used.

        Audited like any other write even though it changes nothing in force,
        because "who uploaded the list that was later activated" is a question
        the activation record alone cannot answer.
        """
        command_id = new_uuid7()
        moment = require_utc(now)
        key = f"{manifest.universe_code}:{manifest.effective_date.isoformat()}"
        await self._ledger.open(
            LedgerEntry(
                id=command_id,
                actor_email=operator.email,
                source_ip=source_ip,
                action=STAGE,
                target_type=TARGET_TYPE,
                target_key=key,
                payload={
                    "member_count": len(manifest.members),
                    "digest": manifest.content_digest.hex(),
                    "source_name": manifest.provenance.name,
                },
                expected_digest=manifest.content_digest,
                started_at=moment,
            )
        )
        try:
            async with self._sessions() as session:
                stored = await UniverseAuthorities(session).stage(
                    manifest, staged_at=moment, staged_by=operator.email
                )
                await self._ledger.succeed(
                    session,
                    command_id=command_id,
                    result_code="STAGED",
                    result={
                        "snapshot_id": str(stored.id),
                        "universe_code": stored.universe_code,
                        "member_count": stored.member_count,
                    },
                    completed_at=moment,
                )
                await session.commit()
        except Exception as error:
            await self._ledger.fail(
                command_id=command_id,
                result_code=_failure_code(error),
                completed_at=moment,
            )
            raise
        return stored

    async def facts(self, snapshot_id: UUID) -> ActivationFacts:
        """What activating this snapshot would do, read from the tables.

        The difference is computed here rather than taken from the page, so
        the approval is bound to what the database holds.
        """
        async with self._sessions() as session:
            repository = UniverseAuthorities(session)
            staged = await repository.snapshot(snapshot_id)
            if staged is None:
                raise UniverseUploadRefused("저장되지 않은 스냅샷입니다.")
            if staged.activated_at is not None:
                raise UniverseUploadRefused("이미 활성화된 스냅샷입니다.")
            current = await repository.active(staged.universe_code)
            candidate = await repository.manifest(snapshot_id)
            previous = (
                None if current is None else await repository.manifest(current.id)
            )
            await session.rollback()
        assert candidate is not None

        # With no authority in force, every member is an addition: activating
        # this is the whole of the change.
        added: tuple[str, ...] = tuple(sorted(candidate.symbols))
        removed: tuple[str, ...] = ()
        changed: tuple[str, ...] = ()
        if previous is not None:
            difference = compare(previous, candidate)
            added, removed, changed = (
                difference.added,
                difference.removed,
                difference.changed,
            )
        return ActivationFacts(
            snapshot_id=snapshot_id,
            universe_code=staged.universe_code,
            effective_date=staged.effective_date.isoformat(),
            member_count=staged.member_count,
            digest=staged.content_digest.hex(),
            current_effective_date=(
                None if current is None else current.effective_date.isoformat()
            ),
            current_member_count=None if current is None else current.member_count,
            added=added,
            removed=removed,
            changed=changed,
        )

    async def activate(
        self, command: ActivationCommand, *, session_id: str
    ) -> StoredSnapshot:
        """Put a staged snapshot in force, behind the second password."""
        facts = await self.facts(command.snapshot_id)
        key = f"{facts.universe_code}:{facts.effective_date}"
        await self._ledger.open(
            LedgerEntry(
                id=command.id,
                actor_email=command.operator.email,
                source_ip=command.source_ip,
                action=ACTIVATE,
                target_type=TARGET_TYPE,
                target_key=key,
                payload={"correlation_id": command.correlation_id},
                expected_digest=facts.digest_bytes(),
                started_at=command.requested_at,
            )
        )
        try:
            await self._approvals.consume(
                command.approval_id,
                approval_for(
                    session_id=session_id, operator=command.operator, facts=facts
                ),
            )
            async with self._sessions() as session:
                stored = await UniverseAuthorities(session).activate(
                    command.snapshot_id,
                    activated_at=command.requested_at,
                    activated_by=command.operator.email,
                )
                await self._ledger.succeed(
                    session,
                    command_id=command.id,
                    result_code="ACTIVATED",
                    result=facts.as_details(),
                    completed_at=command.requested_at,
                )
                # The authority that takes effect and the record that it did
                # commit together.
                await session.commit()
        except Exception as error:
            await self._ledger.fail(
                command_id=command.id,
                result_code=_failure_code(error),
                completed_at=command.requested_at,
            )
            raise
        return stored


_FAILURE_CODES: dict[type[BaseException], str] = {
    UniverseUploadRefused: "UNIVERSE_UPLOAD_REFUSED",
    UniverseAuthorityError: "UNIVERSE_AUTHORITY_REFUSED",
    UniverseManifestError: "UNIVERSE_MANIFEST_INVALID",
}


def _failure_code(error: BaseException) -> str:
    """A stable code, because a message can be reworded and a grep cannot."""
    return _FAILURE_CODES.get(type(error), "UNEXPECTED_ERROR")


__all__ = (
    "ACTIVATE",
    "MAXIMUM_UPLOAD_BYTES",
    "STAGE",
    "TARGET_TYPE",
    "ActivationCommand",
    "ActivationFacts",
    "MySqlUniverseCommands",
    "UniverseUploadRefused",
    "approval_for",
    "new_activation_command",
    "read_upload",
)
