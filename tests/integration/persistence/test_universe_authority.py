"""Keeping a universe honest across a change of authority.

The value of this table is the past. Anything can store today's constituent
list; what a decision made three weeks ago needs is the list published then,
and that only survives if activation supersedes rather than overwrites. These
drive the whole lifecycle through a real MySQL so that the check constraints -
which are where the rules actually live - get a chance to refuse.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from conftest import integration_database_url
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.application.universe_manifest import parse_manifest
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.universe import (
    UniverseSnapshotMemberRow,
    UniverseSnapshotRow,
)
from autotrader.persistence.mysql.repositories.universe import (
    UniverseAuthorities,
    UniverseAuthorityError,
)

STAGED = datetime(2026, 8, 31, 6, 0, tzinfo=UTC)
OPERATOR = "operator@example.com"
AUGUST = date(2026, 8, 31)
SEPTEMBER = date(2026, 9, 30)


def _drive(scenario: object) -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("MySQL is required for integration tests")

    async def run() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await _clear(sessions)
            await scenario(sessions)  # type: ignore[operator]
        finally:
            await _clear(sessions)
            await engine.dispose()

    asyncio.run(run())


async def _clear(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        await session.execute(delete(UniverseSnapshotMemberRow))
        await session.execute(delete(UniverseSnapshotRow))
        await session.commit()


def _document(
    *,
    effective_date: date = AUGUST,
    members: list[dict[str, object]] | None = None,
) -> str:
    return json.dumps(
        {
            "universe_code": "KOSPI200",
            "effective_date": effective_date.isoformat(),
            "source": {
                "name": "KRX",
                "reference": f"kospi200-{effective_date.isoformat()}",
                "published_at": "2026-08-30T09:00:00+00:00",
            },
            "members": members
            or [
                {"symbol": "005930", "common_stock": True, "sector": "IT"},
                {"symbol": "005935", "common_stock": False, "sector": "IT"},
            ],
        }
    )


async def _stage(
    sessions: async_sessionmaker[AsyncSession],
    *,
    effective_date: date = AUGUST,
    members: list[dict[str, object]] | None = None,
    staged_at: datetime = STAGED,
) -> UUID:
    async with sessions() as session:
        stored = await UniverseAuthorities(session).stage(
            parse_manifest(_document(effective_date=effective_date, members=members)),
            staged_at=staged_at,
            staged_by=OPERATOR,
        )
        await session.commit()
        return stored.id


async def _activate(
    sessions: async_sessionmaker[AsyncSession],
    snapshot_id: UUID,
    *,
    at: datetime = STAGED + timedelta(hours=1),
) -> None:
    async with sessions() as session:
        await UniverseAuthorities(session).activate(
            snapshot_id, activated_at=at, activated_by=OPERATOR
        )
        await session.commit()


@pytest.mark.integration
def test_a_staged_snapshot_is_not_yet_the_authority() -> None:
    """Section 11.5 asks the operator to compare before choosing, which is
    only possible while the new list is stored and not yet in force."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        await _stage(sessions)

        async with sessions() as session:
            repository = UniverseAuthorities(session)
            assert await repository.active("KOSPI200") is None
            assert len(await repository.history("KOSPI200")) == 1

    _drive(scenario)


@pytest.mark.integration
def test_activation_puts_one_in_force_and_records_who() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        snapshot_id = await _stage(sessions)
        await _activate(sessions, snapshot_id)

        async with sessions() as session:
            active = await UniverseAuthorities(session).active("KOSPI200")

        assert active is not None
        assert active.id == snapshot_id
        assert active.activated_by == OPERATOR
        assert active.is_active is True

    _drive(scenario)


@pytest.mark.integration
def test_the_replaced_authority_is_superseded_rather_than_deleted() -> None:
    """This is the whole reason for the table. Overwriting August would make
    every August decision unauditable."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        august = await _stage(sessions)
        await _activate(sessions, august)
        september = await _stage(
            sessions,
            effective_date=SEPTEMBER,
            members=[{"symbol": "000660", "common_stock": True, "sector": "IT"}],
        )
        await _activate(sessions, september, at=STAGED + timedelta(days=30))

        async with sessions() as session:
            repository = UniverseAuthorities(session)
            active = await repository.active("KOSPI200")
            history = await repository.history("KOSPI200")

        assert active is not None and active.id == september
        assert [item.id for item in history] == [september, august]
        replaced = history[1]
        assert replaced.superseded_at is not None
        assert replaced.is_active is False
        # And its members are still there to answer with.
        assert replaced.member_count == 2

    _drive(scenario)


@pytest.mark.integration
def test_a_past_date_is_answered_from_the_list_in_force_then() -> None:
    """005930 left the index in September. Asking about an August trade has
    to see August's answer."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        august = await _stage(sessions)
        await _activate(sessions, august)
        september = await _stage(
            sessions,
            effective_date=SEPTEMBER,
            members=[{"symbol": "000660", "common_stock": True, "sector": "IT"}],
        )
        await _activate(sessions, september, at=STAGED + timedelta(days=30))

        async with sessions() as session:
            repository = UniverseAuthorities(session)
            back_then = await repository.membership(
                "KOSPI200", symbol="005930", as_of=date(2026, 9, 10)
            )
            now = await repository.membership(
                "KOSPI200", symbol="005930", as_of=date(2026, 10, 1)
            )

        assert (back_then.member, back_then.effective_date) == (True, AUGUST)
        assert (now.member, now.effective_date) == (False, SEPTEMBER)

    _drive(scenario)


@pytest.mark.integration
def test_a_preferred_line_is_a_member_and_not_a_common_share() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        await _activate(sessions, await _stage(sessions))

        async with sessions() as session:
            preferred = await UniverseAuthorities(session).membership(
                "KOSPI200", symbol="005935", as_of=AUGUST
            )

        assert (preferred.member, preferred.common_stock) == (True, False)

    _drive(scenario)


@pytest.mark.integration
def test_no_authority_is_not_the_same_as_an_excluded_symbol() -> None:
    """Both answer False. Only `authority_available` says which happened, and
    treating the first as the second would stand the strategy aside for a
    reason nobody published."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        await _stage(sessions)  # staged, never activated

        async with sessions() as session:
            answer = await UniverseAuthorities(session).membership(
                "KOSPI200", symbol="005930", as_of=AUGUST
            )

        assert answer.member is False
        assert answer.authority_available is False

    _drive(scenario)


@pytest.mark.integration
def test_a_date_before_any_authority_has_none() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        await _activate(sessions, await _stage(sessions))

        async with sessions() as session:
            answer = await UniverseAuthorities(session).membership(
                "KOSPI200", symbol="005930", as_of=date(2026, 1, 1)
            )

        assert answer.authority_available is False

    _drive(scenario)


@pytest.mark.integration
def test_two_snapshots_for_one_date_are_refused() -> None:
    """A correction has to replace the row, not sit beside it looking equally
    authoritative."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        await _stage(sessions)

        with pytest.raises(UniverseAuthorityError, match="already has a snapshot"):
            await _stage(sessions)

    _drive(scenario)


@pytest.mark.integration
def test_activating_the_same_snapshot_twice_is_refused() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        snapshot_id = await _stage(sessions)
        await _activate(sessions, snapshot_id)

        with pytest.raises(UniverseAuthorityError, match="already been activated"):
            await _activate(sessions, snapshot_id, at=STAGED + timedelta(days=1))

    _drive(scenario)


@pytest.mark.integration
def test_an_older_list_cannot_replace_a_newer_one() -> None:
    """Moving the authority backwards is how a universe silently reverts."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        september = await _stage(sessions, effective_date=SEPTEMBER)
        await _activate(sessions, september)
        august = await _stage(sessions, effective_date=AUGUST)

        with pytest.raises(UniverseAuthorityError, match="older than the active"):
            await _activate(sessions, august, at=STAGED + timedelta(days=2))

    _drive(scenario)


@pytest.mark.integration
def test_the_database_refuses_two_active_snapshots() -> None:
    """The repository holds this rule, and so does the schema, because a stray
    SQL client is held to the second one only."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        first = await _stage(sessions)
        await _activate(sessions, first)
        second = await _stage(sessions, effective_date=SEPTEMBER)

        async with sessions() as session:
            row = await session.get(UniverseSnapshotRow, second)
            assert row is not None
            row.activated_at = STAGED + timedelta(days=1)
            row.activated_by = OPERATOR
            row.active_marker = "ACTIVE"
            with pytest.raises(IntegrityError):
                await session.commit()

    _drive(scenario)


@pytest.mark.integration
def test_the_database_refuses_an_activation_before_staging() -> None:
    """A row written around the repository is still held to the lifecycle."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        snapshot_id = await _stage(sessions)

        async with sessions() as session:
            row = await session.get(UniverseSnapshotRow, snapshot_id)
            assert row is not None
            row.activated_at = STAGED - timedelta(days=1)
            row.activated_by = OPERATOR
            row.active_marker = "ACTIVE"
            with pytest.raises((IntegrityError, OperationalError)):
                await session.commit()

    _drive(scenario)


@pytest.mark.integration
def test_the_database_refuses_an_equity_member_with_no_share_class() -> None:
    """The question has an answer for a listed share, and a row that leaves
    it blank turns NOT_COMMON_STOCK_AS_OF into a fact nobody recorded."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        snapshot_id = await _stage(sessions)

        async with sessions() as session:
            session.add(
                UniverseSnapshotMemberRow(
                    snapshot_id=snapshot_id,
                    universe_code="KOSPI200",
                    symbol="000270",
                    common_stock=None,
                    sector_classification="Autos",
                )
            )
            with pytest.raises((IntegrityError, OperationalError)):
                await session.commit()

    _drive(scenario)


@pytest.mark.integration
def test_the_stored_members_come_back_in_symbol_order() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        snapshot_id = await _stage(sessions)

        async with sessions() as session:
            members = await UniverseAuthorities(session).members(snapshot_id)

        assert [member.symbol for member in members] == ["005930", "005935"]
        assert members[1].common_stock is False

    _drive(scenario)


@pytest.mark.integration
def test_the_digest_is_stored_as_uploaded() -> None:
    """The screen shows it and section 11.8 folds it into readiness, so it has
    to be the digest of what is actually in the table."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        manifest = parse_manifest(_document())
        snapshot_id = await _stage(sessions)

        async with sessions() as session:
            stored = await UniverseAuthorities(session).history("KOSPI200")

        assert stored[0].id == snapshot_id
        assert stored[0].content_digest == manifest.content_digest

    _drive(scenario)
