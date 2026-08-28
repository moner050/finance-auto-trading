"""Accumulating the series the percentile is ranked against.

Deribit publishes no history of the put-call ratio, so this table is the only
place that series can exist. What it must not do is answer with a percentile
before it has one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from conftest import integration_database_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.repositories.pessimism import (
    DailyMeasures,
    MarketPessimism,
)

NOW = datetime(2026, 8, 28, 6, 0, tzinfo=UTC)
START = date(2026, 1, 1)


def _drive(scenario: object) -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("MySQL is required for integration tests")

    async def run() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await scenario(sessions)  # type: ignore[operator]
        finally:
            await engine.dispose()

    asyncio.run(run())


async def _record(
    sessions: async_sessionmaker[AsyncSession],
    *,
    days: int,
    with_put_call: bool = True,
) -> date:
    """A rising series, so today ends up the highest of them."""
    last = START
    async with sessions() as session:
        repository = MarketPessimism(session)
        for offset in range(days):
            last = START + timedelta(days=offset)
            await repository.record(
                DailyMeasures(
                    exchange_date=last,
                    realised_volatility=Decimal(offset + 1) / Decimal(1000),
                    breadth_advancing=offset + 1,
                    breadth_declining=100,
                    breadth_unchanged=0,
                    calls_volume=Decimal(100) if with_put_call else None,
                    puts_volume=(Decimal(offset + 1) if with_put_call else None),
                ),
                now=NOW,
            )
        await session.commit()
    return last


@pytest.mark.integration
def test_a_short_history_yields_no_percentile() -> None:
    """Ranking today against three other days and calling it a percentile is
    how a number nobody measured gets into a trading decision."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        last = await _record(sessions, days=10)

        async with sessions() as session:
            found = await MarketPessimism(session).pessimism(through=last)
            await session.rollback()

        assert found.volatility_percentile is None
        assert found.breadth_percentile is None
        assert found.put_call_percentile is None
        # The date is still reported: something was measured, just not enough.
        assert found.completed_date == last

    _drive(scenario)


@pytest.mark.integration
def test_enough_history_yields_all_three() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        last = await _record(sessions, days=60)

        async with sessions() as session:
            found = await MarketPessimism(session).pessimism(through=last)
            await session.rollback()

        # The series rises, so the newest day is the highest of the sixty.
        assert found.volatility_percentile == Decimal(1)
        assert found.breadth_percentile == Decimal(1)
        assert found.put_call_percentile == Decimal(1)

    _drive(scenario)


@pytest.mark.integration
def test_a_measure_no_venue_answered_stays_absent() -> None:
    """The other two are still ranked. A venue being down costs one series."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        last = await _record(sessions, days=60, with_put_call=False)

        async with sessions() as session:
            found = await MarketPessimism(session).pessimism(through=last)
            await session.rollback()

        assert found.volatility_percentile == Decimal(1)
        assert found.breadth_percentile == Decimal(1)
        assert found.put_call_percentile is None

    _drive(scenario)


@pytest.mark.integration
def test_recording_a_day_again_corrects_it_rather_than_doubling_it() -> None:
    """A day is measured once it is over, so a second reading is a
    correction."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        async with sessions() as session:
            repository = MarketPessimism(session)
            await repository.record(
                DailyMeasures(exchange_date=START, realised_volatility=Decimal("0.01")),
                now=NOW,
            )
            await repository.record(
                DailyMeasures(exchange_date=START, realised_volatility=Decimal("0.02")),
                now=NOW,
            )
            await session.commit()

        async with sessions() as session:
            series = await MarketPessimism(session).series(through=START)
            await session.rollback()

        assert len(series) == 1
        assert series[0].realised_volatility == Decimal("0.02")

    _drive(scenario)


@pytest.mark.integration
def test_a_row_that_observed_nothing_is_refused_by_the_database() -> None:
    """The check is in the schema, so a writer that skipped the dataclass is
    held to it too."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        from autotrader.persistence.mysql.models.pessimism import (
            MarketPessimismDailyRow,
        )
        from autotrader.shared.ids import new_uuid7

        async with sessions() as session:
            session.add(
                MarketPessimismDailyRow(
                    id=new_uuid7(),
                    exchange_date=date(2026, 2, 1),
                    realised_volatility=None,
                    breadth_advancing=None,
                    breadth_declining=None,
                    breadth_unchanged=None,
                    calls_volume=None,
                    puts_volume=None,
                    captured_at=NOW,
                )
            )
            with pytest.raises(Exception, match="ck_market_pessimism_daily_not_empty"):
                await session.commit()
            await session.rollback()

    _drive(scenario)


@pytest.mark.integration
def test_backfill_does_not_overwrite_a_put_call_reading() -> None:
    """Backfill rebuilds breadth and volatility, which can be recomputed at
    any time. It must not replace the put-call reading, which is the only
    copy: no venue keeps a history of it."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        async with sessions() as session:
            await MarketPessimism(session).record(
                DailyMeasures(
                    exchange_date=START,
                    calls_volume=Decimal("44592.0"),
                    puts_volume=Decimal("14563.3"),
                ),
                now=NOW,
            )
            await session.commit()

        async with sessions() as session:
            written = await MarketPessimism(session).record_if_absent(
                DailyMeasures(
                    exchange_date=START,
                    realised_volatility=Decimal("0.02"),
                    breadth_advancing=1,
                    breadth_declining=2,
                    breadth_unchanged=0,
                ),
                now=NOW,
            )
            await session.commit()

        async with sessions() as session:
            series = await MarketPessimism(session).series(through=START)
            await session.rollback()

        assert written is False
        assert series[0].put_call_ratio is not None
        # And the day was left alone entirely rather than half updated.
        assert series[0].realised_volatility is None

    _drive(scenario)


@pytest.mark.integration
def test_backfill_writes_a_day_nothing_recorded() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        async with sessions() as session:
            written = await MarketPessimism(session).record_if_absent(
                DailyMeasures(
                    exchange_date=START,
                    realised_volatility=Decimal("0.02"),
                    breadth_advancing=1,
                    breadth_declining=2,
                    breadth_unchanged=0,
                ),
                now=NOW,
            )
            await session.commit()

        async with sessions() as session:
            series = await MarketPessimism(session).series(through=START)
            await session.rollback()

        assert written is True
        assert series[0].breadth_share == Decimal(1) / Decimal(3)
        assert series[0].put_call_ratio is None

    _drive(scenario)
