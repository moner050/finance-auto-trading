"""Claiming a session, gathering what the day left, and closing it.

The evidence is counted from the tables the loop already writes, not supplied
by the caller. A completion that trusted numbers handed to it would let the
screen decide it was ready, which is the one thing section 17 says the screen
cannot do.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.execution.promotion.models import (
    PromotionBlocker,
    PromotionMode,
    PromotionSession,
    PromotionState,
    SessionEvidence,
    SessionStatus,
    promotion_state,
    verify,
)
from autotrader.persistence.mysql.models.david_v6 import DavidV6DecisionRow
from autotrader.persistence.mysql.models.operations import OpsIncident
from autotrader.persistence.mysql.models.orders import PersistedOrder
from autotrader.persistence.mysql.models.promotion import PromotionSessionRow
from autotrader.persistence.mysql.models.reconciliation import (
    PersistedReconciliationDiff,
    PersistedReconciliationRun,
)
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc

# An exchange date is a local trading day, but every stored timestamp is UTC.
# Until a calendar says otherwise for each venue, a session covers the UTC day,
# and the window is stated here rather than assumed at each query.
_DAY = timedelta(days=1)


class PromotionRefusedError(RuntimeError):
    """Raised when a session cannot be claimed or completed as asked."""


@dataclass(frozen=True, slots=True)
class SessionView:
    """One session and, if it is still open, what stands in its way."""

    session: PromotionSession
    evidence: SessionEvidence
    blockers: tuple[PromotionBlocker, ...]


def evidence_digest(evidence: SessionEvidence) -> bytes:
    return sha256(
        json.dumps(
            {
                "decision_count": evidence.decision_count,
                "order_count": evidence.order_count,
                "blocking_incident_count": evidence.blocking_incident_count,
                "blocking_reconciliation_count": (
                    evidence.blocking_reconciliation_count
                ),
                "unresolved_unknown_count": evidence.unresolved_unknown_count,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).digest()


class PromotionSessions:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self,
        *,
        binding_id: UUID,
        account_id: UUID,
        manifest_id: UUID,
        mode: PromotionMode,
        exchange_date: date,
        now: datetime,
    ) -> PromotionSession:
        moment = require_utc(now)
        if type(mode) is not PromotionMode:
            raise TypeError("mode must be an exact PromotionMode")
        existing = await self._row(
            binding_id=binding_id, mode=mode, exchange_date=exchange_date
        )
        if existing is not None:
            # The unique key would refuse this anyway; saying which day is
            # already claimed beats surfacing a constraint name.
            raise PromotionRefusedError(
                f"{exchange_date.isoformat()} 은 이미 {mode.value} 세션으로 "
                "청구되어 있습니다."
            )
        row = PromotionSessionRow(
            id=new_uuid7(),
            binding_id=binding_id,
            account_id=account_id,
            manifest_id=manifest_id,
            mode=mode.value,
            exchange_date=exchange_date,
            status=SessionStatus.CLAIMED.value,
            claimed_at=moment,
            completed_at=None,
            decision_count=0,
            order_count=0,
            blocking_incident_count=0,
            blocking_reconciliation_count=0,
            unresolved_unknown_count=0,
            evidence_digest=None,
        )
        self._session.add(row)
        await self._session.flush()
        return _view(row)

    async def evidence_for(
        self, *, account_id: UUID, exchange_date: date
    ) -> SessionEvidence:
        """What the day left behind, counted from what the loop wrote.

        Five counts in one statement rather than five statements. They are
        independent and each was costing a round trip, which on a database
        that answers `SELECT 1` in thirty milliseconds is a hundred and fifty
        milliseconds of waiting per session - and the promotion screen asks
        for this once per session per binding.

        Scalar subqueries rather than concurrency: the counts share a session,
        and an AsyncSession is not safe to use from two tasks at once.
        """
        start = datetime.combine(exchange_date, time.min, tzinfo=UTC)
        end = start + _DAY
        counts = (
            await self._session.execute(
                select(
                    # By when the decision was generated, which is when the
                    # strategy actually evaluated, not when the evidence
                    # behind it was completed.
                    select(func.count(DavidV6DecisionRow.id))
                    .where(
                        DavidV6DecisionRow.generated_at >= start,
                        DavidV6DecisionRow.generated_at < end,
                    )
                    .scalar_subquery()
                    .label("decisions"),
                    select(func.count(PersistedOrder.id))
                    .where(
                        PersistedOrder.account_id == account_id,
                        PersistedOrder.created_at >= start,
                        PersistedOrder.created_at < end,
                    )
                    .scalar_subquery()
                    .label("orders"),
                    select(func.count(OpsIncident.id))
                    .where(
                        OpsIncident.severity == "BLOCKING",
                        OpsIncident.status == "OPEN",
                        OpsIncident.created_at >= start,
                        OpsIncident.created_at < end,
                    )
                    .scalar_subquery()
                    .label("incidents"),
                    select(func.count(PersistedReconciliationDiff.id))
                    .join(
                        PersistedReconciliationRun,
                        PersistedReconciliationRun.id
                        == PersistedReconciliationDiff.run_id,
                    )
                    .where(
                        PersistedReconciliationRun.account_id == account_id,
                        PersistedReconciliationDiff.severity == "BLOCKING",
                        PersistedReconciliationDiff.status == "OPEN",
                        PersistedReconciliationDiff.created_at >= start,
                        PersistedReconciliationDiff.created_at < end,
                    )
                    .scalar_subquery()
                    .label("diffs"),
                    select(func.count(PersistedOrder.id))
                    .where(
                        PersistedOrder.account_id == account_id,
                        PersistedOrder.status == "UNKNOWN",
                        PersistedOrder.created_at >= start,
                        PersistedOrder.created_at < end,
                    )
                    .scalar_subquery()
                    .label("unknown"),
                )
            )
        ).one()
        return SessionEvidence(
            decision_count=int(counts.decisions or 0),
            order_count=int(counts.orders or 0),
            blocking_incident_count=int(counts.incidents or 0),
            blocking_reconciliation_count=int(counts.diffs or 0),
            unresolved_unknown_count=int(counts.unknown or 0),
        )

    async def complete(
        self,
        *,
        session_id: UUID,
        now: datetime,
        today: date,
    ) -> PromotionSession:
        moment = require_utc(now)
        row = await self._session.scalar(
            select(PromotionSessionRow)
            .where(PromotionSessionRow.id == session_id)
            .with_for_update()
        )
        if row is None:
            raise PromotionRefusedError("저장되지 않은 세션입니다.")
        if row.status == SessionStatus.COMPLETE.value:
            raise PromotionRefusedError("이미 완료된 세션입니다.")
        evidence = await self.evidence_for(
            account_id=row.account_id, exchange_date=row.exchange_date
        )
        blockers = verify(
            evidence,
            mode=PromotionMode(row.mode),
            exchange_date=row.exchange_date,
            today=today,
        )
        if blockers:
            raise PromotionRefusedError(
                "확인되지 않은 manifest 는 완료할 수 없습니다: "
                + ", ".join(blocker.value for blocker in blockers)
            )
        row.status = SessionStatus.COMPLETE.value
        row.completed_at = moment
        row.decision_count = evidence.decision_count
        row.order_count = evidence.order_count
        row.blocking_incident_count = evidence.blocking_incident_count
        row.blocking_reconciliation_count = evidence.blocking_reconciliation_count
        row.unresolved_unknown_count = evidence.unresolved_unknown_count
        row.evidence_digest = evidence_digest(evidence)
        await self._session.flush()
        return _view(row)

    async def timeline(
        self, *, binding_id: UUID, today: date
    ) -> tuple[SessionView, ...]:
        """Every session for a binding, newest date first, with its blockers."""
        rows = (
            await self._session.scalars(
                select(PromotionSessionRow)
                .where(PromotionSessionRow.binding_id == binding_id)
                .order_by(
                    PromotionSessionRow.exchange_date.desc(),
                    PromotionSessionRow.mode,
                )
            )
        ).all()
        views: list[SessionView] = []
        for row in rows:
            if row.status == SessionStatus.COMPLETE.value:
                # A completed session reports what it closed with. Recounting
                # it would make history depend on today's tables.
                evidence = _stored_evidence(row)
                blockers: tuple[PromotionBlocker, ...] = ()
            else:
                evidence = await self.evidence_for(
                    account_id=row.account_id, exchange_date=row.exchange_date
                )
                blockers = verify(
                    evidence,
                    mode=PromotionMode(row.mode),
                    exchange_date=row.exchange_date,
                    today=today,
                )
            views.append(
                SessionView(session=_view(row), evidence=evidence, blockers=blockers)
            )
        return tuple(views)

    async def state(self, *, binding_id: UUID, manifest_id: UUID) -> PromotionState:
        rows = (
            await self._session.scalars(
                select(PromotionSessionRow).where(
                    PromotionSessionRow.binding_id == binding_id
                )
            )
        ).all()
        return promotion_state(
            tuple(_view(row) for row in rows), manifest_id=manifest_id
        )

    async def _row(
        self, *, binding_id: UUID, mode: PromotionMode, exchange_date: date
    ) -> PromotionSessionRow | None:
        return await self._session.scalar(
            select(PromotionSessionRow).where(
                PromotionSessionRow.binding_id == binding_id,
                PromotionSessionRow.mode == mode.value,
                PromotionSessionRow.exchange_date == exchange_date,
            )
        )

    async def _count(self, statement: object) -> int:
        return int(await self._session.scalar(statement) or 0)  # type: ignore[arg-type]


def _stored_evidence(row: PromotionSessionRow) -> SessionEvidence:
    return SessionEvidence(
        decision_count=int(row.decision_count),
        order_count=int(row.order_count),
        blocking_incident_count=int(row.blocking_incident_count),
        blocking_reconciliation_count=int(row.blocking_reconciliation_count),
        unresolved_unknown_count=int(row.unresolved_unknown_count),
    )


def _view(row: PromotionSessionRow) -> PromotionSession:
    return PromotionSession(
        id=row.id,
        binding_id=row.binding_id,
        account_id=row.account_id,
        manifest_id=row.manifest_id,
        mode=PromotionMode(row.mode),
        exchange_date=row.exchange_date,
        status=SessionStatus(row.status),
        claimed_at=require_utc(row.claimed_at),
        completed_at=(
            None if row.completed_at is None else require_utc(row.completed_at)
        ),
    )


__all__ = (
    "PromotionRefusedError",
    "PromotionSessions",
    "SessionView",
    "evidence_digest",
)
