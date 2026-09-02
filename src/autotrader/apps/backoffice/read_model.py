"""What the operations screen shows, and nothing else.

These are purpose-built projections, not a window onto the tables. Section 10.2
of the backoffice design draws the line: read code may join whatever it needs,
but what comes back carries no ciphertext, no nonce, no API key, no token, and
no raw account identifier. Returning an ORM row would put every one of those a
single template expression away.

Everything here is a read. Nothing on this path may authorize anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.core import CoreExchange, CoreInstrument
from autotrader.persistence.mysql.models.david_v6 import (
    DavidV6BlockerRow,
    DavidV6DecisionRow,
    DavidV6IndicatorRow,
)
from autotrader.persistence.mysql.models.operations import (
    OpsIncident,
    OpsTradingControl,
)
from autotrader.persistence.mysql.models.positions import Position
from autotrader.persistence.mysql.models.reconciliation import (
    PersistedReconciliationDiff,
)
from autotrader.persistence.mysql.repositories.protection import ProtectionRepository

DEFAULT_DECISION_LIMIT = 20
# A day, so an operator arriving in the morning sees the night.
ACTIVITY_HOURS = 24
DEFAULT_INCIDENT_LIMIT = 20


@dataclass(frozen=True, slots=True)
class ControlView:
    scope_type: str
    scope_key: str
    armed: bool
    kill_switch_level: str
    owner_runtime_instance_id: UUID | None
    lease_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class MatchedIndicatorView:
    """One indicator the engine actually found, and whether it had to."""

    key: str
    mandatory: bool


@dataclass(frozen=True, slots=True)
class DecisionView:
    id: UUID
    market: str
    family: str
    grade: str
    side: str
    generated_at: datetime
    matched_indicator_count: int
    blocker_count: int
    # What the decision was about. A screen that lists twenty verdicts and no
    # symbol tells the operator that the loop is running and nothing else -
    # and this system already runs more than one instrument on one market.
    instrument_code: str
    # Where that instrument trades. A decision is not made under an account,
    # so there is no credential provider on it; the exchange is the honest
    # attribution and the one that is actually recorded.
    exchange_code: str
    # Kept as the stored codes. Section 12 wants the operator's language on
    # screen with the stable reason code still visible beside it, and the code
    # is the half that survives a translation nobody updated.
    blockers: tuple[str, ...]
    # The other half of the same question. Blockers say what stopped it;
    # these say what it did find, which is what distinguishes a market that
    # offered nothing from a setup that fell one gate short.
    indicators: tuple[MatchedIndicatorView, ...]


@dataclass(frozen=True, slots=True)
class PositionView:
    instrument_code: str
    quantity: Decimal
    average_cost: Decimal
    currency: str | None
    observed_at: datetime
    protected: bool


@dataclass(frozen=True, slots=True)
class DriftView:
    diff_key: str
    instrument_code: str | None
    severity: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IncidentView:
    severity: str
    reason_code: str
    scope_type: str | None
    scope_key: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ActivityBucket:
    """One hour of evaluation, including the hours with nothing in them.

    An empty hour is the most important thing this series can say - it means
    the loop was not running - so the buckets are generated from the clock and
    filled from the query rather than taken from whatever the query returned.
    """

    hour: datetime
    decisions: int
    accepted: int
    best_matched: int | None
    fewest_blockers: int | None

    @property
    def idle(self) -> bool:
        return self.decisions == 0


@dataclass(frozen=True, slots=True)
class OperationsView:
    controls: tuple[ControlView, ...]
    decisions: tuple[DecisionView, ...]
    activity: tuple[ActivityBucket, ...]
    positions: tuple[PositionView, ...]
    drifts: tuple[DriftView, ...]
    incidents: tuple[IncidentView, ...]

    @property
    def armed(self) -> bool:
        """The same rule the loop applies, so the screen cannot say otherwise."""
        return bool(self.controls) and all(
            control.armed and control.kill_switch_level == "NONE"
            for control in self.controls
        )

    @property
    def busiest_hour(self) -> int:
        """The tallest bar, so the chart has something to scale against.

        One rather than zero when nothing has happened: the bars are a
        percentage of this, and an empty day should draw a flat axis rather
        than divide by zero.
        """
        return max((bucket.decisions for bucket in self.activity), default=0) or 1

    @property
    def evaluated_recently(self) -> int:
        return sum(bucket.decisions for bucket in self.activity)

    @property
    def accepted_recently(self) -> int:
        return sum(bucket.accepted for bucket in self.activity)

    @property
    def closest_recently(self) -> ActivityBucket | None:
        """The hour that came nearest to a setup.

        Nearest means most indicators matched, and fewest blockers breaks the
        tie. Both are needed: a pass can be blocked by one thing having
        matched nothing, and that is not close.
        """
        scored = [
            bucket
            for bucket in self.activity
            if bucket.best_matched is not None and bucket.fewest_blockers is not None
        ]
        if not scored:
            return None
        return max(
            scored,
            key=lambda bucket: (
                bucket.best_matched or 0,
                -(bucket.fewest_blockers or 0),
                bucket.hour,
            ),
        )

    @property
    def idle_hours(self) -> int:
        return sum(1 for bucket in self.activity if bucket.idle)

    @property
    def unprotected_positions(self) -> tuple[PositionView, ...]:
        return tuple(position for position in self.positions if not position.protected)


class OperationsReadModel:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(
        self,
        *,
        account_id: UUID,
        decision_limit: int = DEFAULT_DECISION_LIMIT,
        incident_limit: int = DEFAULT_INCIDENT_LIMIT,
        now: datetime | None = None,
    ) -> OperationsView:
        return OperationsView(
            controls=await self.controls(),
            decisions=await self.decisions(limit=decision_limit),
            activity=await self.activity(now=now),
            positions=await self.positions(account_id=account_id),
            drifts=await self.open_drifts(),
            incidents=await self.open_incidents(limit=incident_limit),
        )

    async def controls(self) -> tuple[ControlView, ...]:
        rows = await self._session.scalars(
            select(OpsTradingControl).order_by(
                OpsTradingControl.scope_type, OpsTradingControl.scope_key
            )
        )
        return tuple(
            ControlView(
                scope_type=row.scope_type,
                scope_key=row.scope_key,
                armed=row.armed,
                kill_switch_level=row.kill_switch_level,
                owner_runtime_instance_id=row.owner_runtime_instance_id,
                lease_expires_at=row.expires_at,
            )
            for row in rows
        )

    async def decisions(self, *, limit: int) -> tuple[DecisionView, ...]:
        _require_limit(limit)
        rows = (
            await self._session.execute(
                select(
                    DavidV6DecisionRow,
                    CoreInstrument.code,
                    CoreExchange.code,
                )
                .join(
                    CoreInstrument,
                    CoreInstrument.id == DavidV6DecisionRow.instrument_id,
                )
                .join(CoreExchange, CoreExchange.id == CoreInstrument.exchange_id)
                .order_by(
                    DavidV6DecisionRow.generated_at.desc(),
                    DavidV6DecisionRow.id.desc(),
                )
                .limit(limit)
            )
        ).all()
        identifiers = {row.id for row, _, _ in rows}
        blockers = await self._blockers(identifiers)
        indicators = await self._indicators(identifiers)
        return tuple(
            DecisionView(
                id=row.id,
                market=row.market,
                family=row.family,
                grade=row.grade,
                side=row.side,
                generated_at=row.generated_at,
                matched_indicator_count=row.matched_indicator_count,
                blocker_count=row.blocker_count,
                instrument_code=instrument_code,
                exchange_code=exchange_code,
                blockers=blockers.get(row.id, ()),
                indicators=indicators.get(row.id, ()),
            )
            for row, instrument_code, exchange_code in rows
        )

    async def _indicators(
        self, decision_ids: set[UUID]
    ) -> dict[UUID, tuple[MatchedIndicatorView, ...]]:
        """One query for the page, in the order the engine recorded them."""
        if not decision_ids:
            return {}
        rows = (
            await self._session.execute(
                select(
                    DavidV6IndicatorRow.decision_id,
                    DavidV6IndicatorRow.indicator_key,
                    DavidV6IndicatorRow.mandatory,
                )
                .where(DavidV6IndicatorRow.decision_id.in_(decision_ids))
                .order_by(DavidV6IndicatorRow.decision_id, DavidV6IndicatorRow.ordinal)
            )
        ).all()
        collected: dict[UUID, list[MatchedIndicatorView]] = {}
        for decision_id, key, mandatory in rows:
            collected.setdefault(decision_id, []).append(
                MatchedIndicatorView(key=key, mandatory=bool(mandatory))
            )
        return {key: tuple(value) for key, value in collected.items()}

    async def activity(
        self, *, hours: int = ACTIVITY_HOURS, now: datetime | None = None
    ) -> tuple[ActivityBucket, ...]:
        """Evaluation per hour, oldest first, with the empty hours kept.

        Aggregated in the database rather than by reading the decisions back:
        a day of five-minute passes is around three hundred rows, and the
        screen wants five numbers per hour, not the rows.

        The clock is the caller's so a test can state the hour it means. The
        buckets are UTC because everything else on this screen prints UTC, and
        one panel on local time beside tables on UTC is worse than either.
        """
        if type(hours) is not int or not 1 <= hours <= 168:
            raise ValueError("activity window must be between 1 and 168 hours")
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        latest = moment.replace(minute=0, second=0, microsecond=0)
        earliest = latest - timedelta(hours=hours - 1)
        bucket = func.date_format(DavidV6DecisionRow.generated_at, "%Y-%m-%d %H")
        rows = (
            await self._session.execute(
                select(
                    bucket.label("hour"),
                    func.count().label("decisions"),
                    func.sum(
                        case((DavidV6DecisionRow.grade != "REJECT", 1), else_=0)
                    ).label("accepted"),
                    func.max(DavidV6DecisionRow.matched_indicator_count),
                    func.min(DavidV6DecisionRow.blocker_count),
                )
                .where(DavidV6DecisionRow.generated_at >= earliest)
                .group_by(bucket)
            )
        ).all()
        found = {
            str(row[0]): (
                int(row[1]),
                int(row[2] or 0),
                None if row[3] is None else int(row[3]),
                None if row[4] is None else int(row[4]),
            )
            for row in rows
        }
        buckets: list[ActivityBucket] = []
        for step in range(hours):
            hour = earliest + timedelta(hours=step)
            decisions, accepted, matched, blockers = found.get(
                hour.strftime("%Y-%m-%d %H"), (0, 0, None, None)
            )
            buckets.append(
                ActivityBucket(
                    hour=hour,
                    decisions=decisions,
                    accepted=accepted,
                    best_matched=matched,
                    fewest_blockers=blockers,
                )
            )
        return tuple(buckets)

    async def _blockers(self, decision_ids: set[UUID]) -> dict[UUID, tuple[str, ...]]:
        """One query for the page, rather than one per decision."""
        if not decision_ids:
            return {}
        rows = (
            await self._session.execute(
                select(DavidV6BlockerRow.decision_id, DavidV6BlockerRow.blocker_code)
                .where(DavidV6BlockerRow.decision_id.in_(decision_ids))
                .order_by(DavidV6BlockerRow.decision_id, DavidV6BlockerRow.ordinal)
            )
        ).all()
        collected: dict[UUID, list[str]] = {}
        for decision_id, blocker_code in rows:
            collected.setdefault(decision_id, []).append(blocker_code)
        return {key: tuple(value) for key, value in collected.items()}

    async def positions(self, *, account_id: UUID) -> tuple[PositionView, ...]:
        rows = (
            await self._session.execute(
                select(Position, CoreInstrument.code)
                .join(CoreInstrument, CoreInstrument.id == Position.instrument_id)
                .where(Position.account_id == account_id, Position.quantity != 0)
                .order_by(CoreInstrument.code)
            )
        ).all()
        protected = await ProtectionRepository(self._session).protected_instruments(
            account_id=account_id,
            among={position.instrument_id for position, _ in rows},
        )
        return tuple(
            PositionView(
                instrument_code=code,
                quantity=position.quantity,
                average_cost=position.average_cost,
                currency=position.currency,
                observed_at=position.observed_at,
                protected=position.instrument_id in protected,
            )
            for position, code in rows
        )

    async def open_drifts(self) -> tuple[DriftView, ...]:
        rows = (
            await self._session.execute(
                select(PersistedReconciliationDiff, CoreInstrument.code)
                .outerjoin(
                    CoreInstrument,
                    CoreInstrument.id == PersistedReconciliationDiff.instrument_id,
                )
                .where(PersistedReconciliationDiff.status == "OPEN")
                .order_by(PersistedReconciliationDiff.created_at.desc())
            )
        ).all()
        return tuple(
            DriftView(
                diff_key=diff.diff_key,
                instrument_code=code,
                severity=diff.severity,
                created_at=diff.created_at,
            )
            for diff, code in rows
        )

    async def open_incidents(self, *, limit: int) -> tuple[IncidentView, ...]:
        _require_limit(limit)
        rows = await self._session.scalars(
            select(OpsIncident)
            .where(OpsIncident.status == "OPEN")
            .order_by(OpsIncident.created_at.desc(), OpsIncident.id.desc())
            .limit(limit)
        )
        return tuple(
            IncidentView(
                severity=row.severity,
                reason_code=row.reason_code,
                scope_type=row.scope_type,
                scope_key=row.scope_key,
                created_at=row.created_at,
            )
            for row in rows
        )


def _require_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")


__all__ = (
    "ACTIVITY_HOURS",
    "DEFAULT_DECISION_LIMIT",
    "DEFAULT_INCIDENT_LIMIT",
    "ActivityBucket",
    "ControlView",
    "DecisionView",
    "DriftView",
    "IncidentView",
    "MatchedIndicatorView",
    "OperationsReadModel",
    "OperationsView",
    "PositionView",
)
