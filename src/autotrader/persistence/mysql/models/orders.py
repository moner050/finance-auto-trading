from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BINARY,
    BigInteger,
    CheckConstraint,
    Computed,
    ForeignKeyConstraint,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary
from autotrader.shared.ids import new_uuid7


class PersistedOrder(CoreBase):
    __tablename__ = "exec_order"
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id"],
            ["exec_account.id"],
            name="fk_exec_order_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["risk_decision_id", "order_intent_id"],
            ["risk_decision.id", "risk_decision.order_intent_id"],
            name="fk_exec_order_decision_intent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["instrument_id"],
            ["core_instrument.id"],
            name="fk_exec_order_instrument",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["order_intent_id"],
            ["exec_order_intent.id"],
            name="fk_exec_order_intent",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "aggregate_version >= 0", name="ck_exec_order_aggregate_version"
        ),
        CheckConstraint(
            "(requested_quantity > 0) and (filled_quantity >= 0)",
            name="ck_exec_order_quantities",
        ),
        UniqueConstraint("order_intent_id", name="uq_exec_order_intent"),
        UniqueConstraint(
            "account_id",
            "broker_client_order_id",
            name="uq_exec_order_account_client_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    order_intent_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    risk_decision_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    broker_client_order_id: Mapped[str] = mapped_column(
        String(128, collation="ascii_bin"), nullable=False
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_style: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    # A protective stop rests until the market reaches this price.
    trigger_price: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    aggregate_version: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class PersistedOrderCommand(CoreBase):
    __tablename__ = "exec_order_command"
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id"],
            ["exec_account.id"],
            name="fk_exec_order_command_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["order_id", "authority_class"],
            [
                "exec_order_command_authority.order_id",
                "exec_order_command_authority.authority_class",
            ],
            name="fk_exec_order_command_authority",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["instrument_id"],
            ["core_instrument.id"],
            name="fk_exec_order_command_instrument",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["order_id"],
            ["exec_order.id"],
            name="fk_exec_order_command_order",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["replaces_command_id"],
            ["exec_order_command.id"],
            name="fk_exec_order_command_replaces",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "((command_type = 'SUBMIT') and (authority_class in "
            "('SUBMIT_NEW_EXPOSURE','SUBMIT_STRICT_REDUCTION'))) or ((command_type = "
            "'CANCEL') and (authority_class = 'CANCEL')) or ((command_type = "
            "'REPLACE') and (authority_class = 'REPLACE_NON_INCREASING'))",
            name="ck_exec_order_command_authority_terms",
        ),
        CheckConstraint(
            "(quantity > 0) and (target_aggregate_version >= 0)",
            name="ck_exec_order_command_terms",
        ),
        CheckConstraint(
            "command_type in ('SUBMIT','CANCEL','REPLACE')",
            name="ck_exec_order_command_type",
        ),
        UniqueConstraint(
            "order_id", "command_sequence", name="uq_exec_order_command_sequence"
        ),
        UniqueConstraint("idempotency_key", name="uq_exec_order_command_idempotency"),
        UniqueConstraint(
            "order_id", "submit_once_marker", name="uq_exec_order_submit_once"
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    order_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    command_type: Mapped[str] = mapped_column(String(16), nullable=False)
    command_sequence: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    target_aggregate_version: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(256, collation="ascii_bin"), nullable=False
    )
    canonical_payload_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    submit_once_marker: Mapped[str | None] = mapped_column(
        String(1),
        Computed("CASE WHEN command_type = 'SUBMIT' THEN 'Y' ELSE NULL END"),
    )
    broker_client_order_id: Mapped[str] = mapped_column(
        String(128, collation="ascii_bin"), nullable=False
    )
    target_broker_order_id: Mapped[str | None] = mapped_column(
        String(128, collation="ascii_bin"), nullable=True
    )
    replaces_command_id: Mapped[UUID | None] = mapped_column(
        UuidBinary(), nullable=True
    )
    origin_type: Mapped[str] = mapped_column(String(16), nullable=False)
    authority_class: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_runtime_instance_id: Mapped[UUID | None] = mapped_column(
        UuidBinary(), nullable=True
    )
    fencing_token: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    not_after: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_style: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    # A protective stop rests until the market reaches this price.
    trigger_price: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    time_in_force: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    dispatch_attempted_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    result_state: Mapped[str | None] = mapped_column(String(24), nullable=True)


class PersistedOrderCommandAuthority(CoreBase):
    __tablename__ = "exec_order_command_authority"
    __table_args__ = (
        ForeignKeyConstraint(
            ["order_id"],
            ["exec_order.id"],
            name="fk_exec_order_command_authority_order",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "authority_class in "
            "('SUBMIT_NEW_EXPOSURE','SUBMIT_STRICT_REDUCTION','CANCEL','REPLACE_NON_INCREASING')",
            name="ck_exec_order_command_authority_class",
        ),
        UniqueConstraint(
            "order_id", "authority_class", name="uq_exec_order_command_authority"
        ),
    )

    order_id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    authority_class: Mapped[str] = mapped_column(String(32), primary_key=True)


class PersistedBrokerOrderLink(CoreBase):
    __tablename__ = "exec_broker_order_link"
    __table_args__ = (
        UniqueConstraint(
            "order_id", "link_sequence", name="uq_exec_broker_link_sequence"
        ),
        ForeignKeyConstraint(
            ["broker_id"],
            ["exec_broker.id"],
            name="fk_exec_broker_link_broker",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["order_id"],
            ["exec_order.id"],
            name="fk_exec_broker_link_order",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "broker_id", "broker_order_id", name="uq_exec_broker_link_broker_order"
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    order_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    broker_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    broker_order_id: Mapped[str] = mapped_column(
        String(128, collation="ascii_bin"), nullable=False
    )
    link_sequence: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    exposure_bearing: Mapped[bool] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)


class PersistedBrokerOrderStatusEvent(CoreBase):
    __tablename__ = "exec_broker_order_status_event"
    __table_args__ = (
        UniqueConstraint(
            "broker_id",
            "account_id",
            "source_partition",
            "dedupe_key",
            name="uq_exec_broker_status_identity",
        ),
        ForeignKeyConstraint(
            ["account_id"],
            ["exec_account.id"],
            name="fk_exec_broker_status_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["broker_id"],
            ["exec_broker.id"],
            name="fk_exec_broker_status_broker",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["order_id"],
            ["exec_order.id"],
            name="fk_exec_broker_status_order",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    order_id: Mapped[UUID | None] = mapped_column(UuidBinary(), nullable=True)
    broker_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    account_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    source_partition: Mapped[str] = mapped_column(
        String(128, collation="ascii_bin"), nullable=False
    )
    dedupe_key: Mapped[str] = mapped_column(
        String(256, collation="ascii_bin"), nullable=False
    )
    canonical_payload_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    broker_order_id: Mapped[str] = mapped_column(
        String(128, collation="ascii_bin"), nullable=False
    )
    broker_client_order_id: Mapped[str] = mapped_column(
        String(128, collation="ascii_bin"), nullable=False
    )
    raw_status: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    cumulative_filled_quantity: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    source_sequence: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class PersistedOrderStatusWatermark(CoreBase):
    __tablename__ = "exec_order_status_watermark"
    __table_args__ = (
        ForeignKeyConstraint(
            ["order_id"],
            ["exec_order.id"],
            name="fk_exec_order_status_watermark_order",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "last_contiguous_sequence >= 0",
            name="ck_exec_order_status_watermark_sequence",
        ),
        UniqueConstraint(
            "order_id", "source_partition", name="uq_exec_order_status_watermark"
        ),
    )

    order_id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    source_partition: Mapped[str] = mapped_column(
        String(128, collation="ascii_bin"), primary_key=True
    )
    last_contiguous_sequence: Mapped[int] = mapped_column(BigInteger(), nullable=False)


class PersistedOrderEvent(CoreBase):
    __tablename__ = "exec_order_event"
    __table_args__ = (
        ForeignKeyConstraint(
            ["order_id"],
            ["exec_order.id"],
            name="fk_exec_order_event_order",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "order_id", "aggregate_version", name="uq_exec_order_event_version"
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    order_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    aggregate_version: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    raw_status: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
