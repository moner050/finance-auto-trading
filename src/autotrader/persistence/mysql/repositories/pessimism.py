"""Recording the day's measures, and ranking them when they are read.

The regime wants three percentiles. Each is a rank of today within a history
that grows, so ranking happens here, on read, over whatever has accumulated.

Until enough days exist a percentile is not available, and this says so by
returning None rather than ranking today against three other days and calling
the answer a percentile. The strategy already knows what to do with a missing
input: it blocks. A fabricated rank would instead let it trade on a number
nobody measured.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.pessimism import MarketPessimismDailyRow
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.regime import PessimismInputs

# A rank over fewer days than this describes the sample, not the market. Sixty
# trading days is the shortest window over which a decile means anything, and
# the strategy reads deciles.
MINIMUM_HISTORY = 60


@dataclass(frozen=True, slots=True)
class DailyMeasures:
    """What one day was observed to be. Any part may be absent."""

    exchange_date: date
    realised_volatility: Decimal | None = None
    breadth_advancing: int | None = None
    breadth_declining: int | None = None
    breadth_unchanged: int | None = None
    calls_volume: Decimal | None = None
    puts_volume: Decimal | None = None

    def __post_init__(self) -> None:
        if type(self.exchange_date) is not date:
            raise TypeError("exchange_date must be an exact date")
        counts = (
            self.breadth_advancing,
            self.breadth_declining,
            self.breadth_unchanged,
        )
        if any(value is None for value in counts) and any(
            value is not None for value in counts
        ):
            raise ValueError("breadth is three counts or none of them")
        volumes = (self.calls_volume, self.puts_volume)
        if any(value is None for value in volumes) and any(
            value is not None for value in volumes
        ):
            raise ValueError("the put-call reading is both volumes or neither")
        if all(
            value is None
            for value in (
                self.realised_volatility,
                self.breadth_advancing,
                self.calls_volume,
            )
        ):
            raise ValueError("a day that observed nothing is not a measurement")

    @property
    def breadth_share(self) -> Decimal | None:
        if self.breadth_advancing is None:
            return None
        total = (
            self.breadth_advancing
            + (self.breadth_declining or 0)
            + (self.breadth_unchanged or 0)
        )
        if total == 0:
            return None
        return Decimal(self.breadth_advancing) / Decimal(total)

    @property
    def put_call_ratio(self) -> Decimal | None:
        if self.calls_volume is None or self.puts_volume is None:
            return None
        if self.calls_volume == 0:
            # A day with no call volume has no ratio; see the reader.
            return None
        return self.puts_volume / self.calls_volume


def rank(values: Sequence[Decimal], *, today: Decimal) -> Decimal:
    """Where today sits among the values, as a share at or below it."""
    if not values:
        raise ValueError("a rank needs values")
    at_or_below = sum(1 for value in values if value <= today)
    return Decimal(at_or_below) / Decimal(len(values))


class MarketPessimism:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, measures: DailyMeasures, *, now: datetime) -> UUID:
        """One row per day. Recording the same day again replaces it.

        A day is measured once it is over, so a second reading of the same day
        is a correction rather than a second observation.
        """
        moment = require_utc(now)
        existing = await self._session.scalar(
            select(MarketPessimismDailyRow)
            .where(MarketPessimismDailyRow.exchange_date == measures.exchange_date)
            .with_for_update()
        )
        row = existing or MarketPessimismDailyRow(
            id=new_uuid7(), exchange_date=measures.exchange_date
        )
        row.realised_volatility = measures.realised_volatility
        row.breadth_advancing = measures.breadth_advancing
        row.breadth_declining = measures.breadth_declining
        row.breadth_unchanged = measures.breadth_unchanged
        row.calls_volume = measures.calls_volume
        row.puts_volume = measures.puts_volume
        row.captured_at = moment
        if existing is None:
            self._session.add(row)
        await self._session.flush()
        return row.id

    async def record_if_absent(self, measures: DailyMeasures, *, now: datetime) -> bool:
        """Write a day only if nothing is recorded for it yet.

        Backfill reconstructs what the venues still publish — breadth and
        volatility — and cannot reconstruct the put-call ratio, which no venue
        keeps a history of. Using `record` would replace a day that already
        held a put-call reading with one that does not, destroying the only
        copy of it to fill in two measures that could be recomputed at any
        time.
        """
        existing = await self._session.scalar(
            select(MarketPessimismDailyRow.id).where(
                MarketPessimismDailyRow.exchange_date == measures.exchange_date
            )
        )
        if existing is not None:
            return False
        await self.record(measures, now=now)
        return True

    async def series(self, *, through: date) -> tuple[DailyMeasures, ...]:
        rows = (
            await self._session.scalars(
                select(MarketPessimismDailyRow)
                .where(MarketPessimismDailyRow.exchange_date <= through)
                .order_by(MarketPessimismDailyRow.exchange_date)
            )
        ).all()
        return tuple(
            DailyMeasures(
                exchange_date=row.exchange_date,
                realised_volatility=row.realised_volatility,
                breadth_advancing=row.breadth_advancing,
                breadth_declining=row.breadth_declining,
                breadth_unchanged=row.breadth_unchanged,
                calls_volume=row.calls_volume,
                puts_volume=row.puts_volume,
            )
            for row in rows
        )

    async def pessimism(
        self, *, through: date, minimum_history: int = MINIMUM_HISTORY
    ) -> PessimismInputs:
        """The three percentiles, or None for each that has too little history.

        The completed date is the most recent day any measure was recorded
        for. A percentile of a day the market has not finished would rank a
        partial observation against complete ones.
        """
        history = await self.series(through=through)
        if not history:
            return PessimismInputs(
                completed_date=None,
                volatility_percentile=None,
                put_call_percentile=None,
                breadth_percentile=None,
            )
        return PessimismInputs(
            completed_date=history[-1].exchange_date,
            volatility_percentile=_percentile(
                [day.realised_volatility for day in history], minimum_history
            ),
            breadth_percentile=_percentile(
                [day.breadth_share for day in history], minimum_history
            ),
            put_call_percentile=_percentile(
                [day.put_call_ratio for day in history], minimum_history
            ),
        )


def _percentile(
    values: Sequence[Decimal | None], minimum_history: int
) -> Decimal | None:
    """Rank the latest present value, if enough of them are present.

    Days where the measure was absent are skipped rather than filled. Carrying
    the previous day forward would put a value into the history that nobody
    observed, and it would be counted in every rank thereafter.
    """
    present = [value for value in values if value is not None]
    if len(present) < minimum_history:
        return None
    return rank(present, today=present[-1])


__all__ = (
    "MINIMUM_HISTORY",
    "DailyMeasures",
    "MarketPessimism",
    "rank",
)
