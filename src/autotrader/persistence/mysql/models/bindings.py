"""Provider-to-account bindings.

A binding names one provider acting for one account in one environment. It is
the unit the reconciliation runs attach to, and the unit a LIVE activation is
granted against.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary
from autotrader.shared.ids import new_uuid7


class ProviderAccountBinding(CoreBase):
    __tablename__ = "exec_provider_account_binding"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "provider_code",
            "environment",
            "revision",
            name="uq_exec_provider_account_binding_revision",
        ),
        # The composite targets the reconciliation runs and the recovery lease
        # point at, so a run can never attach to another account's binding.
        UniqueConstraint(
            "id", "account_id", name="uq_exec_provider_account_binding_id_account"
        ),
        UniqueConstraint(
            "id",
            "account_id",
            "provider_code",
            "environment",
            name="uq_exec_provider_account_binding_exact_scope",
        ),
        CheckConstraint(
            "provider_code IN ('TOSS', 'KIS', 'BINANCE') "
            "AND environment IN ('PAPER', 'LIVE') "
            "AND revision > 0 "
            "AND ((provider_code = 'TOSS' AND account_seq > 0) "
            "OR (provider_code IN ('KIS', 'BINANCE') AND account_seq IS NULL))",
            name="ck_exec_provider_account_binding_scope",
        ),
        ForeignKeyConstraint(
            ["account_id", "broker_id", "environment"],
            ["exec_account.id", "exec_account.broker_id", "exec_account.environment"],
            name="fk_exec_provider_account_binding_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["broker_id", "provider_code"],
            ["exec_broker.id", "exec_broker.code"],
            name="fk_exec_provider_account_binding_broker",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    broker_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    provider_code: Mapped[str] = mapped_column(
        String(64, collation="utf8mb4_bin"), nullable=False
    )
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    account_seq: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    revision: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean(), nullable=False)


__all__ = ("ProviderAccountBinding",)
