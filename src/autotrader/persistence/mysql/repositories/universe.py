"""Storing universe authorities, and answering what was true on a given day.

Two operations write here, and neither of them can express less than a whole
snapshot. `stage` puts an uploaded manifest where it can be compared but not
used; `activate` makes one the authority in force and demotes the one it
replaces. There is nothing that adds a symbol.

Reading is where the point of keeping history shows up. `membership` takes a
date and finds the snapshot that was in force as of that date, not the one in
force now. A decision made three weeks ago was filtered by the list published
then, and evaluating it against today's list would report a trade as
ineligible that was eligible when it was taken - or, worse, the reverse.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.application.universe_manifest import (
    UNIVERSE_CODES,
    UniverseManifest,
    UniverseMember,
    UniverseProvenance,
)
from autotrader.persistence.mysql.models.universe import (
    UniverseSnapshotMemberRow,
    UniverseSnapshotRow,
)
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc

ACTIVE = "ACTIVE"


class UniverseAuthorityError(RuntimeError):
    """Raised when a universe operation would leave the authority wrong."""


@dataclass(frozen=True, slots=True)
class StoredSnapshot:
    """One snapshot as the screens and the loop see it."""

    id: UUID
    universe_code: str
    effective_date: date
    content_digest: bytes
    member_count: int
    provenance: UniverseProvenance
    staged_at: datetime
    staged_by: str
    activated_at: datetime | None
    activated_by: str | None
    superseded_at: datetime | None

    @property
    def is_active(self) -> bool:
        return self.activated_at is not None and self.superseded_at is None


@dataclass(frozen=True, slots=True)
class Membership:
    """What the authority in force on one date says about one symbol.

    `member` and `common_stock` are the two separate answers the strategy's
    universe filter asks for, and they are kept apart because they block for
    different reasons.
    """

    universe_code: str
    as_of: date
    symbol: str
    snapshot_id: UUID | None
    effective_date: date | None
    member: bool
    common_stock: bool
    sector: str | None

    @property
    def authority_available(self) -> bool:
        return self.snapshot_id is not None


class UniverseAuthorities:
    """The universe tables, as the one way to read and change them."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def stage(
        self, manifest: UniverseManifest, *, staged_at: datetime, staged_by: str
    ) -> StoredSnapshot:
        """Store an uploaded manifest without putting it in force.

        Staging and activating are separate because section 11.5 asks the
        operator to compare the two before choosing, and a snapshot that took
        effect on upload would leave nothing to compare against.
        """
        if type(manifest) is not UniverseManifest:
            raise TypeError("manifest must be an exact UniverseManifest")
        staged = require_utc(staged_at)
        actor = _actor(staged_by)
        existing = await self._snapshot_on(
            manifest.universe_code, manifest.effective_date
        )
        if existing is not None:
            # A second upload for a published date is a correction, and a
            # correction to an authority already in force is not a staging
            # operation - it would silently change what the loop is reading.
            raise UniverseAuthorityError(
                f"{manifest.universe_code} already has a snapshot for "
                f"{manifest.effective_date.isoformat()}"
            )
        row = UniverseSnapshotRow(
            id=new_uuid7(),
            universe_code=manifest.universe_code,
            effective_date=manifest.effective_date,
            content_digest=manifest.content_digest,
            member_count=len(manifest.members),
            source_name=manifest.provenance.name,
            source_reference=manifest.provenance.reference,
            source_published_at=require_utc(manifest.provenance.published_at),
            staged_at=staged,
            staged_by=actor,
            activated_at=None,
            activated_by=None,
            superseded_at=None,
            active_marker=None,
        )
        self._session.add(row)
        # Before the members, because the composite foreign key points at the
        # snapshot's (id, universe_code) and nothing declares the two tables
        # related, so the unit of work has no reason to order them.
        await self._session.flush()
        self._session.add_all(
            UniverseSnapshotMemberRow(
                id=new_uuid7(),
                snapshot_id=row.id,
                universe_code=manifest.universe_code,
                symbol=member.symbol,
                common_stock=member.common_stock,
                sector_classification=member.sector,
            )
            for member in manifest.members
        )
        await self._session.flush()
        return _stored(row)

    async def activate(
        self, snapshot_id: UUID, *, activated_at: datetime, activated_by: str
    ) -> StoredSnapshot:
        """Put one staged snapshot in force, demoting the one it replaces.

        Both writes happen in the caller's transaction. Half of this - a new
        authority in force with the old one still marked active, or neither -
        is a universe the loop cannot read.
        """
        moment = require_utc(activated_at)
        actor = _actor(activated_by)
        row = await self._session.get(UniverseSnapshotRow, snapshot_id)
        if row is None:
            raise UniverseAuthorityError("no such universe snapshot")
        if row.activated_at is not None:
            raise UniverseAuthorityError(
                "this snapshot has already been activated once"
            )
        if moment < row.staged_at:
            raise UniverseAuthorityError("a snapshot cannot activate before it existed")
        current = await self._active_row(row.universe_code)
        if current is not None:
            if current.effective_date > row.effective_date:
                # Activating an older list over a newer one would move the
                # authority backwards without anything recording that it had.
                raise UniverseAuthorityError(
                    "a snapshot older than the active one cannot replace it"
                )
            current.superseded_at = moment
            current.active_marker = None
        row.activated_at = moment
        row.activated_by = actor
        row.active_marker = ACTIVE
        await self._session.flush()
        return _stored(row)

    async def active(self, universe_code: str) -> StoredSnapshot | None:
        """The authority in force now, or None if none has been activated."""
        row = await self._active_row(_code(universe_code))
        return None if row is None else _stored(row)

    async def history(
        self, universe_code: str, *, limit: int = 50
    ) -> tuple[StoredSnapshot, ...]:
        """Every snapshot of one authority, newest effective date first."""
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        rows = (
            await self._session.scalars(
                select(UniverseSnapshotRow)
                .where(UniverseSnapshotRow.universe_code == _code(universe_code))
                .order_by(
                    UniverseSnapshotRow.effective_date.desc(),
                    UniverseSnapshotRow.staged_at.desc(),
                )
                .limit(limit)
            )
        ).all()
        return tuple(_stored(row) for row in rows)

    async def members(self, snapshot_id: UUID) -> tuple[UniverseMember, ...]:
        """One snapshot's list, in symbol order."""
        rows = (
            await self._session.scalars(
                select(UniverseSnapshotMemberRow)
                .where(UniverseSnapshotMemberRow.snapshot_id == snapshot_id)
                .order_by(UniverseSnapshotMemberRow.symbol)
            )
        ).all()
        return tuple(
            UniverseMember(
                symbol=row.symbol,
                common_stock=row.common_stock,
                sector=row.sector_classification,
            )
            for row in rows
        )

    async def snapshot_in_force(
        self, universe_code: str, *, as_of: date
    ) -> StoredSnapshot | None:
        """The authority that was in force on a date, activated or since
        superseded.

        Superseded rows are eligible on purpose. Yesterday's list is exactly
        what a question about yesterday has to be answered from.
        """
        code = _code(universe_code)
        if type(as_of) is not date:
            raise TypeError("as_of must be an exact date")
        row = await self._session.scalar(
            select(UniverseSnapshotRow)
            .where(
                UniverseSnapshotRow.universe_code == code,
                UniverseSnapshotRow.effective_date <= as_of,
                UniverseSnapshotRow.activated_at.is_not(None),
            )
            .order_by(UniverseSnapshotRow.effective_date.desc())
            .limit(1)
        )
        return None if row is None else _stored(row)

    async def membership(
        self, universe_code: str, *, symbol: str, as_of: date
    ) -> Membership:
        """Whether one symbol was a member, and a common share, on one date.

        Both answers are False when no authority was in force, which is not
        the same as saying the symbol was excluded. `authority_available` is
        what tells those apart, and the caller has to look at it: treating a
        missing authority as a clean exclusion would let the strategy stand
        aside for a reason nobody published.
        """
        code = _code(universe_code)
        snapshot = await self.snapshot_in_force(code, as_of=as_of)
        if snapshot is None:
            return Membership(
                universe_code=code,
                as_of=as_of,
                symbol=symbol,
                snapshot_id=None,
                effective_date=None,
                member=False,
                common_stock=False,
                sector=None,
            )
        row = await self._session.scalar(
            select(UniverseSnapshotMemberRow).where(
                UniverseSnapshotMemberRow.snapshot_id == snapshot.id,
                UniverseSnapshotMemberRow.symbol == symbol,
            )
        )
        return Membership(
            universe_code=code,
            as_of=as_of,
            symbol=symbol,
            snapshot_id=snapshot.id,
            effective_date=snapshot.effective_date,
            member=row is not None,
            # A member with no share class recorded is a perpetual, which is
            # not a common share and is not claimed to be one.
            common_stock=row is not None and row.common_stock is True,
            sector=None if row is None else row.sector_classification,
        )

    async def _active_row(self, universe_code: str) -> UniverseSnapshotRow | None:
        return await self._session.scalar(
            select(UniverseSnapshotRow).where(
                UniverseSnapshotRow.universe_code == universe_code,
                UniverseSnapshotRow.active_marker == ACTIVE,
            )
        )

    async def _snapshot_on(
        self, universe_code: str, effective_date: date
    ) -> UniverseSnapshotRow | None:
        return await self._session.scalar(
            select(UniverseSnapshotRow).where(
                UniverseSnapshotRow.universe_code == universe_code,
                UniverseSnapshotRow.effective_date == effective_date,
            )
        )


def _stored(row: UniverseSnapshotRow) -> StoredSnapshot:
    return StoredSnapshot(
        id=row.id,
        universe_code=row.universe_code,
        effective_date=row.effective_date,
        content_digest=row.content_digest,
        member_count=row.member_count,
        provenance=UniverseProvenance(
            name=row.source_name,
            reference=row.source_reference,
            published_at=require_utc(row.source_published_at),
        ),
        staged_at=require_utc(row.staged_at),
        staged_by=row.staged_by,
        activated_at=(
            None if row.activated_at is None else require_utc(row.activated_at)
        ),
        activated_by=row.activated_by,
        superseded_at=(
            None if row.superseded_at is None else require_utc(row.superseded_at)
        ),
    )


def _code(value: str) -> str:
    if value not in UNIVERSE_CODES:
        raise UniverseAuthorityError(f"{value} is not a known universe")
    return value


def _actor(value: str) -> str:
    if type(value) is not str or not value.strip() or value.strip() != value:
        raise ValueError("the acting operator must be named")
    return value


__all__ = (
    "ACTIVE",
    "Membership",
    "StoredSnapshot",
    "UniverseAuthorities",
    "UniverseAuthorityError",
)
