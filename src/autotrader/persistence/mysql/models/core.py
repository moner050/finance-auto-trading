from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary
from autotrader.shared.ids import new_uuid7


class CoreBase(DeclarativeBase):
    pass


class CoreDataSource(CoreBase):
    __tablename__ = "core_data_source"
    __table_args__ = (
        UniqueConstraint("code", name="uq_core_data_source_code"),
        CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE')", name="ck_core_data_source_status"
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    code: Mapped[str] = mapped_column(
        String(32, collation="utf8mb4_bin"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=lambda: datetime.now(UTC), nullable=False
    )


class CoreMarket(CoreBase):
    __tablename__ = "core_market"
    __table_args__ = (
        UniqueConstraint("code", name="uq_core_market_code"),
        CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE')", name="ck_core_market_status"
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    code: Mapped[str] = mapped_column(
        String(16, collation="utf8mb4_bin"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=lambda: datetime.now(UTC), nullable=False
    )


class CoreExchange(CoreBase):
    __tablename__ = "core_exchange"
    __table_args__ = (
        UniqueConstraint("market_id", "code", name="uq_core_exchange_market_code"),
        CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE')", name="ck_core_exchange_status"
        ),
        Index("ix_core_exchange_market_id", "market_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    market_id: Mapped[UUID] = mapped_column(
        UuidBinary(),
        ForeignKey("core_market.id", name="fk_core_exchange_market"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(
        String(32, collation="utf8mb4_bin"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=lambda: datetime.now(UTC), nullable=False
    )


class CoreInstrument(CoreBase):
    __tablename__ = "core_instrument"
    __table_args__ = (
        UniqueConstraint(
            "exchange_id", "code", name="uq_core_instrument_exchange_code"
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'INACTIVE')", name="ck_core_instrument_status"
        ),
        Index("ix_core_instrument_exchange_id", "exchange_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    exchange_id: Mapped[UUID] = mapped_column(
        UuidBinary(),
        ForeignKey("core_exchange.id", name="fk_core_instrument_exchange"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(
        String(64, collation="utf8mb4_bin"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=lambda: datetime.now(UTC), nullable=False
    )
