"""One row per day for the three measures the regime is judged on.

The strategy wants percentiles. Percentiles are not stored, the measures are:
a rank changes every time the history grows, so a stored rank is a number that
was true once. What is stored is what was observed, and the rank is computed
when it is read.

Raw counts rather than the share, and raw volumes rather than the ratio, for
the same reason. A stored share cannot be checked against anything; a stored
advancing-declining pair can.

Each measure is nullable on purpose. Breadth comes from one venue and the
put-call ratio from another, and a day where one of them was unreachable is a
day with a gap in that series and a real reading in the others. Discarding the
whole row would throw away what was observed to avoid recording what was not.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary
from autotrader.shared.ids import new_uuid7


class MarketPessimismDailyRow(CoreBase):
    __tablename__ = "market_pessimism_daily"
    __table_args__ = (
        UniqueConstraint("exchange_date", name="uq_market_pessimism_daily_day"),
        CheckConstraint(
            "(realised_volatility IS NULL OR realised_volatility >= 0) "
            "AND (breadth_advancing IS NULL OR breadth_advancing >= 0) "
            "AND (breadth_declining IS NULL OR breadth_declining >= 0) "
            "AND (breadth_unchanged IS NULL OR breadth_unchanged >= 0) "
            "AND (calls_volume IS NULL OR calls_volume >= 0) "
            "AND (puts_volume IS NULL OR puts_volume >= 0)",
            name="ck_market_pessimism_daily_non_negative",
        ),
        CheckConstraint(
            # Breadth is three counts or none of them. Two thirds of a count
            # is not a smaller measurement, it is an unusable one.
            "(breadth_advancing IS NULL AND breadth_declining IS NULL "
            "AND breadth_unchanged IS NULL) OR "
            "(breadth_advancing IS NOT NULL AND breadth_declining IS NOT NULL "
            "AND breadth_unchanged IS NOT NULL)",
            name="ck_market_pessimism_daily_breadth_whole",
        ),
        CheckConstraint(
            "(calls_volume IS NULL AND puts_volume IS NULL) OR "
            "(calls_volume IS NOT NULL AND puts_volume IS NOT NULL)",
            name="ck_market_pessimism_daily_put_call_whole",
        ),
        CheckConstraint(
            # A row that observed nothing is not a record of a day, it is a
            # record of having looked.
            "realised_volatility IS NOT NULL OR breadth_advancing IS NOT NULL "
            "OR calls_volume IS NOT NULL",
            name="ck_market_pessimism_daily_not_empty",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    exchange_date: Mapped[date] = mapped_column(Date(), nullable=False)
    # The standard deviation of recent daily returns, as observed. Ranked on
    # read, never on write.
    realised_volatility: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    breadth_advancing: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    breadth_declining: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    breadth_unchanged: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    calls_volume: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    puts_volume: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


__all__ = ("MarketPessimismDailyRow",)
