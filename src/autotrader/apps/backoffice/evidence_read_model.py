"""What the providers said, and how long ago they said it.

Section 11.6 asks for reconciliation state, redacted mismatches, freshness, and
Binance permission evidence. All four are read from rows the loop and the
provider captures already wrote; none of it is fetched here, because a screen
that calls a broker turns looking at the evidence into producing more of it.

Redacted means what it says. A mismatch is shown as its kind, the ids on either
side, and the digests — never a provider payload, and never a secret. The
digests are what make two runs comparable without either being readable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.persistence.mysql.models.accounts import Account
from autotrader.persistence.mysql.models.binance_usdm import (
    BinanceUsdmConfigurationFactRow,
    BinanceUsdmReconciliationRunRow,
)
from autotrader.persistence.mysql.models.reconciliation import (
    PersistedReconciliationDiff,
    PersistedReconciliationRun,
)
from autotrader.persistence.mysql.models.toss_us_reconciliation import (
    TossUsReconciliationRunRow,
)
from autotrader.shared.time import require_utc

# How many of each list a screen can usefully hold. Older rows are still in the
# table; a page that tried to render all of them would stop being readable
# exactly when there was most to read.
RECENT = 20

# Enough to tell two digests apart, and not the digest.
DIGEST_PREFIX = 12


@dataclass(frozen=True, slots=True)
class RunView:
    """One reconciliation run, from whichever pipeline produced it."""

    source: str
    account_alias: str | None
    result: str
    complete: bool
    blocking_diffs: int
    started_at: str
    completed_at: str | None
    age: str
    digest_prefix: str | None

    @property
    def clean(self) -> bool:
        return self.complete and self.blocking_diffs == 0


@dataclass(frozen=True, slots=True)
class MismatchView:
    """One disagreement, named but not quoted."""

    kind: str
    severity: str
    status: str
    internal_order_id: str | None
    broker_order_id: str | None
    instrument_id: str | None
    created_at: str

    @property
    def blocking(self) -> bool:
        return self.severity == "BLOCKING"


@dataclass(frozen=True, slots=True)
class PermissionView:
    """Binance's answer about what this key may do.

    `transfer_out_enabled` is the one worth reading first: a trading key that
    can move funds off the exchange is a different risk from one that cannot,
    and the provider is the only authority on which it is.
    """

    captured_at: str
    age: str
    can_trade: bool
    transfer_out_enabled: bool
    multi_assets_margin: bool
    position_mode: str
    margin_type: str
    leverage: int
    auto_add_margin: bool


@dataclass(frozen=True, slots=True)
class EvidenceView:
    runs: tuple[RunView, ...]
    mismatches: tuple[MismatchView, ...]
    permissions: tuple[PermissionView, ...]
    # Stated on the page rather than left as a blank panel: these are not
    # empty, they are unavailable, and the difference matters.
    unavailable: tuple[str, ...]


UNAVAILABLE = (
    "요청 한도(rate limit)는 트레이더 프로세스 메모리에만 있고 테이블이 없어 "
    "여기서 보여줄 수 없습니다.",
    "일회성 provider 캡처 실행은 여기서 제공하지 않습니다. 증거를 보는 화면이 "
    "증거를 만들기 시작하면 둘을 구분할 수 없게 됩니다.",
)


def _age(moment: datetime, *, now: datetime) -> str:
    """How stale, in words an operator reads at a glance."""
    gap = now - require_utc(moment)
    if gap < timedelta(0):
        # A provider clock ahead of ours. Saying so beats rendering a negative.
        return "미래"
    seconds = int(gap.total_seconds())
    if seconds < 60:
        return f"{seconds}초"
    if seconds < 3600:
        return f"{seconds // 60}분"
    if seconds < 86400:
        return f"{seconds // 3600}시간"
    return f"{seconds // 86400}일"


def _stamp(moment: datetime | None) -> str | None:
    return None if moment is None else require_utc(moment).isoformat(timespec="seconds")


class EvidenceReadModel:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def load(self, *, now: datetime) -> EvidenceView:
        moment = require_utc(now)
        async with self._sessions() as session:
            runs = await self._runs(session, now=moment)
            mismatches = await self._mismatches(session)
            permissions = await self._permissions(session, now=moment)
            await session.rollback()
        return EvidenceView(
            runs=runs,
            mismatches=mismatches,
            permissions=permissions,
            unavailable=UNAVAILABLE,
        )

    async def _runs(
        self, session: AsyncSession, *, now: datetime
    ) -> tuple[RunView, ...]:
        """Three pipelines, one list, newest first.

        Keeping them apart on the screen would ask the operator to remember
        which one covers which account, which is the sort of thing a screen is
        supposed to save them from.
        """
        collected: list[tuple[datetime, RunView]] = []

        aliases: dict[UUID, str] = {
            row[0]: row[1]
            for row in (
                await session.execute(select(Account.id, Account.account_alias))
            ).all()
        }

        loop_rows = (
            await session.scalars(
                select(PersistedReconciliationRun)
                .order_by(PersistedReconciliationRun.started_at.desc())
                .limit(RECENT)
            )
        ).all()
        for row in loop_rows:
            # The count, not whether there is one. "Three blocking
            # mismatches" and "one" are different situations.
            count = int(
                await session.scalar(
                    select(func.count(PersistedReconciliationDiff.id)).where(
                        PersistedReconciliationDiff.run_id == row.id,
                        PersistedReconciliationDiff.severity == "BLOCKING",
                    )
                )
                or 0
            )
            collected.append(
                (
                    require_utc(row.started_at),
                    RunView(
                        source="LOOP",
                        account_alias=aliases.get(row.account_id),
                        result=row.status,
                        complete=row.complete,
                        blocking_diffs=count,
                        started_at=_stamp(row.started_at) or "",
                        completed_at=_stamp(row.completed_at),
                        age=_age(row.started_at, now=now),
                        digest_prefix=row.snapshot_hash.hex()[:DIGEST_PREFIX],
                    ),
                )
            )

        toss_rows = (
            await session.scalars(
                select(TossUsReconciliationRunRow)
                .order_by(TossUsReconciliationRunRow.started_at.desc())
                .limit(RECENT)
            )
        ).all()
        for row in toss_rows:
            collected.append(
                (
                    require_utc(row.started_at),
                    RunView(
                        source="TOSS",
                        account_alias=aliases.get(row.account_id),
                        result=row.result,
                        complete=row.missing_page_count == 0,
                        blocking_diffs=len(row.blockers),
                        started_at=_stamp(row.started_at) or "",
                        completed_at=_stamp(row.completed_at),
                        age=_age(row.started_at, now=now),
                        digest_prefix=(
                            None
                            if row.fact_digest is None
                            else row.fact_digest.hex()[:DIGEST_PREFIX]
                        ),
                    ),
                )
            )

        binance_rows = (
            await session.scalars(
                select(BinanceUsdmReconciliationRunRow)
                .order_by(BinanceUsdmReconciliationRunRow.started_at.desc())
                .limit(RECENT)
            )
        ).all()
        for row in binance_rows:
            collected.append(
                (
                    require_utc(row.started_at),
                    RunView(
                        source="BINANCE",
                        account_alias=aliases.get(row.account_id),
                        result=row.result,
                        complete=not row.blockers,
                        blocking_diffs=len(row.blockers),
                        started_at=_stamp(row.started_at) or "",
                        completed_at=_stamp(row.completed_at),
                        age=_age(row.started_at, now=now),
                        digest_prefix=row.fact_digest.hex()[:DIGEST_PREFIX],
                    ),
                )
            )

        collected.sort(key=lambda pair: pair[0], reverse=True)
        return tuple(view for _, view in collected[:RECENT])

    async def _mismatches(self, session: AsyncSession) -> tuple[MismatchView, ...]:
        """Open ones first: a resolved disagreement is history, an open one is
        the reason the loop is refusing to trade."""
        rows = (
            await session.scalars(
                select(PersistedReconciliationDiff)
                .where(PersistedReconciliationDiff.status == "OPEN")
                .order_by(PersistedReconciliationDiff.created_at.desc())
                .limit(RECENT)
            )
        ).all()
        return tuple(
            MismatchView(
                kind=row.diff_key,
                severity=row.severity,
                status=row.status,
                internal_order_id=(
                    None
                    if row.internal_order_id is None
                    else str(row.internal_order_id)
                ),
                broker_order_id=row.broker_order_id,
                instrument_id=(
                    None if row.instrument_id is None else str(row.instrument_id)
                ),
                created_at=_stamp(row.created_at) or "",
            )
            for row in rows
        )

    async def _permissions(
        self, session: AsyncSession, *, now: datetime
    ) -> tuple[PermissionView, ...]:
        rows = (
            await session.scalars(
                select(BinanceUsdmConfigurationFactRow)
                .order_by(BinanceUsdmConfigurationFactRow.captured_at.desc())
                .limit(RECENT)
            )
        ).all()
        return tuple(
            PermissionView(
                captured_at=_stamp(row.captured_at) or "",
                age=_age(row.captured_at, now=now),
                can_trade=row.can_trade,
                transfer_out_enabled=row.transfer_out_enabled,
                multi_assets_margin=row.multi_assets_margin,
                position_mode=row.position_mode,
                margin_type=row.margin_type,
                leverage=int(row.leverage),
                auto_add_margin=row.auto_add_margin,
            )
            for row in rows
        )


__all__ = (
    "DIGEST_PREFIX",
    "RECENT",
    "UNAVAILABLE",
    "EvidenceReadModel",
    "EvidenceView",
    "MismatchView",
    "PermissionView",
    "RunView",
)
