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
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.core import CoreInstrument
from autotrader.persistence.mysql.models.david_v6 import (
    DavidV6BlockerRow,
    DavidV6DecisionRow,
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
class DecisionView:
    id: UUID
    market: str
    family: str
    grade: str
    side: str
    generated_at: datetime
    matched_indicator_count: int
    # Kept as the stored codes. Section 12 wants the operator's language on
    # screen with the stable reason code still visible beside it, and the code
    # is the half that survives a translation nobody updated.
    blockers: tuple[str, ...]


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
class OperationsView:
    controls: tuple[ControlView, ...]
    decisions: tuple[DecisionView, ...]
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
    ) -> OperationsView:
        return OperationsView(
            controls=await self.controls(),
            decisions=await self.decisions(limit=decision_limit),
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
        rows = list(
            (
                await self._session.scalars(
                    select(DavidV6DecisionRow)
                    .order_by(
                        DavidV6DecisionRow.generated_at.desc(),
                        DavidV6DecisionRow.id.desc(),
                    )
                    .limit(limit)
                )
            ).all()
        )
        blockers = await self._blockers({row.id for row in rows})
        return tuple(
            DecisionView(
                id=row.id,
                market=row.market,
                family=row.family,
                grade=row.grade,
                side=row.side,
                generated_at=row.generated_at,
                matched_indicator_count=row.matched_indicator_count,
                blockers=blockers.get(row.id, ()),
            )
            for row in rows
        )

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
    "DEFAULT_DECISION_LIMIT",
    "DEFAULT_INCIDENT_LIMIT",
    "ControlView",
    "DecisionView",
    "DriftView",
    "IncidentView",
    "OperationsReadModel",
    "OperationsView",
    "PositionView",
)
