"""The promotion timeline, as a screen reads it.

Section 11.7 asks for four things and section 17 constrains all of them: the
back office shows the promotion state and claims sessions, but it does not
decide whether a manifest verified. That answer comes from the domain, through
the repository, over evidence counted from the tables the loop wrote.

So there is no "mark complete" here that takes the operator's word for it. The
complete button asks the repository, and the repository asks the evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.backoffice.display import FULL_PATTERN, in_kst
from autotrader.execution.promotion.models import (
    REQUIRED_SESSIONS,
    PromotionMode,
    SessionStatus,
)
from autotrader.persistence.mysql.models.accounts import Account
from autotrader.persistence.mysql.models.bindings import ProviderAccountBinding
from autotrader.persistence.mysql.models.david_v6 import DavidV6ManifestRow
from autotrader.persistence.mysql.repositories.promotion import PromotionSessions


@dataclass(frozen=True, slots=True)
class SessionRow:
    """One day on the timeline."""

    session_id: UUID
    mode: str
    exchange_date: str
    status: str
    claimed_at: str
    completed_at: str | None
    decision_count: int
    order_count: int
    blockers: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.status == SessionStatus.COMPLETE.value

    @property
    def completable(self) -> bool:
        """Offered only when nothing stands in the way.

        A button that exists in order to refuse teaches the operator to press
        it and read the error, which is the opposite of showing the blockers.
        """
        return not self.complete and not self.blockers


@dataclass(frozen=True, slots=True)
class BindingProgress:
    """One binding's standing against the two-and-two requirement."""

    binding_id: UUID
    account_id: UUID
    account_alias: str
    provider_code: str
    environment: str
    manifest_id: UUID | None
    manifest_version: str | None
    shadow_dates: tuple[str, ...]
    paper_dates: tuple[str, ...]
    shadow_remaining: int
    paper_remaining: int
    ready: bool
    sessions: tuple[SessionRow, ...]


@dataclass(frozen=True, slots=True)
class PromotionView:
    required: int
    bindings: tuple[BindingProgress, ...]
    modes: tuple[str, ...]
    # No manifest means no session can be claimed: a session is a session of
    # some exact strategy build, and there is not one to name.
    manifest_missing: bool


class PromotionReadModel:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def load(self, *, today: date) -> PromotionView:
        async with self._sessions() as session:
            manifest = await session.scalar(
                select(DavidV6ManifestRow).order_by(
                    DavidV6ManifestRow.registered_at.desc()
                )
            )
            manifest_id = None if manifest is None else manifest.id
            manifest_version = None if manifest is None else manifest.strategy_version
            rows = (
                await session.execute(
                    select(ProviderAccountBinding, Account.account_alias)
                    .join(Account, Account.id == ProviderAccountBinding.account_id)
                    .where(ProviderAccountBinding.active.is_(True))
                    .order_by(Account.account_alias)
                )
            ).all()
            repository = PromotionSessions(session)
            bindings: list[BindingProgress] = []
            for binding, alias in rows:
                timeline = await repository.timeline(binding_id=binding.id, today=today)
                state = (
                    None
                    if manifest_id is None
                    else await repository.state(
                        binding_id=binding.id, manifest_id=manifest_id
                    )
                )
                bindings.append(
                    BindingProgress(
                        binding_id=binding.id,
                        account_id=binding.account_id,
                        account_alias=alias,
                        provider_code=binding.provider_code,
                        environment=binding.environment,
                        manifest_id=manifest_id,
                        manifest_version=manifest_version,
                        shadow_dates=(
                            ()
                            if state is None
                            else tuple(
                                item.isoformat()
                                for item in state.shadow.completed_dates
                            )
                        ),
                        paper_dates=(
                            ()
                            if state is None
                            else tuple(
                                item.isoformat() for item in state.paper.completed_dates
                            )
                        ),
                        shadow_remaining=(
                            REQUIRED_SESSIONS
                            if state is None
                            else state.shadow.remaining
                        ),
                        paper_remaining=(
                            REQUIRED_SESSIONS
                            if state is None
                            else state.paper.remaining
                        ),
                        ready=False if state is None else state.ready,
                        sessions=tuple(
                            SessionRow(
                                session_id=view.session.id,
                                mode=view.session.mode.value,
                                exchange_date=view.session.exchange_date.isoformat(),
                                status=view.session.status.value,
                                claimed_at=in_kst(
                                    view.session.claimed_at, FULL_PATTERN
                                ),
                                completed_at=(
                                    None
                                    if view.session.completed_at is None
                                    else in_kst(view.session.completed_at, FULL_PATTERN)
                                ),
                                decision_count=view.evidence.decision_count,
                                order_count=view.evidence.order_count,
                                blockers=tuple(
                                    blocker.value for blocker in view.blockers
                                ),
                            )
                            for view in timeline
                        ),
                    )
                )
            await session.rollback()
        return PromotionView(
            required=REQUIRED_SESSIONS,
            bindings=tuple(bindings),
            modes=tuple(mode.value for mode in PromotionMode),
            manifest_missing=manifest_id is None,
        )


__all__ = (
    "BindingProgress",
    "PromotionReadModel",
    "PromotionView",
    "SessionRow",
)
