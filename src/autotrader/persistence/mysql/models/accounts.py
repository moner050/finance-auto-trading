from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary
from autotrader.shared.ids import new_uuid7


class Broker(CoreBase):
    __tablename__ = "exec_broker"
    __table_args__ = (
        UniqueConstraint("code", name="uq_exec_broker_code"),
        UniqueConstraint("id", "code", name="uq_exec_broker_id_code"),
    )
    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    code: Mapped[str] = mapped_column(
        String(64, collation="utf8mb4_bin"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)


class Account(CoreBase):
    __tablename__ = "exec_account"
    __table_args__ = (
        UniqueConstraint("id", "broker_id", name="uq_exec_account_id_broker"),
        ForeignKeyConstraint(
            ["broker_id"],
            ["exec_broker.id"],
            name="fk_exec_account_broker",
        ),
        UniqueConstraint(
            "broker_id",
            "account_alias",
            "environment",
            name="uq_exec_account_environment",
        ),
        UniqueConstraint(
            "id",
            "broker_id",
            "environment",
            name="uq_exec_account_id_broker_environment",
        ),
        CheckConstraint(
            "environment IN ('PAPER', 'LIVE')",
            name="ck_exec_account_environment",
        ),
    )
    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    broker_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    account_alias: Mapped[str] = mapped_column(
        String(128, collation="utf8mb4_bin"), nullable=False
    )
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    secret_reference: Mapped[str] = mapped_column(String(512), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)


class AccountSnapshot(CoreBase):
    __tablename__ = "exec_account_snapshot"
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id"], ["exec_account.id"], name="fk_exec_account_snapshot_account"
        ),
    )
    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    as_of: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)


class CashSnapshot(CoreBase):
    __tablename__ = "exec_cash_snapshot"
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_snapshot_id"],
            ["exec_account_snapshot.id"],
            name="fk_exec_cash_snapshot_account_snapshot",
        ),
    )
    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    account_snapshot_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
