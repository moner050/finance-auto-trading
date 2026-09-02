"""The three universe authorities, as a screen reads them.

Section 11.5 wants four things on one page: what is in force, what is staged
beside it, how the two differ, and what came before. The difference is the one
that earns its place - a member count agrees on a swap, and a swap is exactly
what an operator is about to activate without noticing.

Nothing here mutates. Section 17 puts the back office outside the authority
chain, so the screen reads the tables the manifest upload wrote and reports
what it finds.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.application.universe_manifest import (
    UNIVERSE_CODES,
    UniverseProvenance,
    compare,
)
from autotrader.apps.backoffice.display import FULL_PATTERN, in_kst
from autotrader.persistence.mysql.repositories.universe import (
    StoredSnapshot,
    UniverseAuthorities,
)

# Ordered for the page rather than sorted, so the two equity authorities sit
# together and the venue that trades on a different calendar sits last.
DISPLAY_ORDER = ("KOSPI200", "SP100", "BINANCE_USDM")


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    """One snapshot on the history table."""

    snapshot_id: UUID
    effective_date: str
    member_count: int
    digest: str
    source_name: str
    source_reference: str
    published_at: str
    staged_at: str
    staged_by: str
    activated_at: str | None
    activated_by: str | None
    superseded_at: str | None

    @property
    def state(self) -> str:
        if self.activated_at is None:
            return "STAGED"
        return "SUPERSEDED" if self.superseded_at else "ACTIVE"

    @property
    def activatable(self) -> bool:
        return self.activated_at is None


@dataclass(frozen=True, slots=True)
class DifferenceRow:
    """What activating the staged snapshot would change."""

    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]
    identical: bool


@dataclass(frozen=True, slots=True)
class AuthorityView:
    """One universe: what is in force, what is waiting, and the history."""

    universe_code: str
    active: SnapshotRow | None
    staged: tuple[SnapshotRow, ...]
    history: tuple[SnapshotRow, ...]
    # Against the active snapshot, and only when there is exactly one staged
    # snapshot to compare. Two candidates would need the operator to say which.
    difference: DifferenceRow | None

    @property
    def has_authority(self) -> bool:
        return self.active is not None


@dataclass(frozen=True, slots=True)
class UniverseView:
    authorities: tuple[AuthorityView, ...]

    @property
    def missing_authorities(self) -> tuple[str, ...]:
        return tuple(
            item.universe_code for item in self.authorities if not item.has_authority
        )


class UniverseReadModel:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def load(self) -> UniverseView:
        async with self._sessions() as session:
            authorities = tuple(
                [
                    await self._authority(session, code)
                    for code in DISPLAY_ORDER
                    if code in UNIVERSE_CODES
                ]
            )
            await session.rollback()
        return UniverseView(authorities=authorities)

    async def _authority(self, session: AsyncSession, code: str) -> AuthorityView:
        repository = UniverseAuthorities(session)
        history = await repository.history(code)
        active_row = next((item for item in history if item.is_active), None)
        staged_rows = tuple(item for item in history if item.activated_at is None)
        difference = None
        if active_row is not None and len(staged_rows) == 1:
            difference = await self._difference(
                repository, previous=active_row, current=staged_rows[0]
            )
        return AuthorityView(
            universe_code=code,
            active=None if active_row is None else _row(active_row),
            staged=tuple(_row(item) for item in staged_rows),
            history=tuple(_row(item) for item in history),
            difference=difference,
        )

    async def _difference(
        self,
        repository: UniverseAuthorities,
        *,
        previous: StoredSnapshot,
        current: StoredSnapshot,
    ) -> DifferenceRow:
        before = await repository.manifest(previous.id)
        after = await repository.manifest(current.id)
        assert before is not None and after is not None
        found = compare(before, after)
        return DifferenceRow(
            added=found.added,
            removed=found.removed,
            changed=found.changed,
            identical=found.identical,
        )


def _row(snapshot: StoredSnapshot) -> SnapshotRow:
    return SnapshotRow(
        snapshot_id=snapshot.id,
        effective_date=snapshot.effective_date.isoformat(),
        member_count=snapshot.member_count,
        # Enough to compare two by eye without filling the page with a hash.
        digest=snapshot.content_digest.hex()[:16],
        source_name=snapshot.provenance.name,
        source_reference=snapshot.provenance.reference,
        published_at=_moment(snapshot.provenance),
        staged_at=in_kst(snapshot.staged_at, FULL_PATTERN),
        staged_by=snapshot.staged_by,
        activated_at=(
            None
            if snapshot.activated_at is None
            else in_kst(snapshot.activated_at, FULL_PATTERN)
        ),
        activated_by=snapshot.activated_by,
        superseded_at=(
            None
            if snapshot.superseded_at is None
            else in_kst(snapshot.superseded_at, FULL_PATTERN)
        ),
    )


def _moment(provenance: UniverseProvenance) -> str:
    return in_kst(provenance.published_at, FULL_PATTERN)


__all__ = (
    "DISPLAY_ORDER",
    "AuthorityView",
    "DifferenceRow",
    "SnapshotRow",
    "UniverseReadModel",
    "UniverseView",
)
