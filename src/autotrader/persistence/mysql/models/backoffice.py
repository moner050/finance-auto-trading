from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BINARY,
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import VARBINARY
from sqlalchemy.orm import Mapped, mapped_column

from autotrader.persistence.mysql.models.core import CoreBase
from autotrader.persistence.mysql.types import UtcDateTime, UuidBinary
from autotrader.shared.ids import new_uuid7


class BackofficeSecretVersionRow(CoreBase):
    __tablename__ = "backoffice_secret_version"
    __table_args__ = (
        UniqueConstraint(
            "logical_name", "version", name="uq_backoffice_secret_version_name_version"
        ),
        UniqueConstraint(
            "id", "logical_name", name="uq_backoffice_secret_version_exact_scope"
        ),
        CheckConstraint(
            "category IN ('PROVIDER_CREDENTIAL', 'ACCOUNT_IDENTIFIER', 'OAUTH')",
            name="ck_backoffice_secret_version_category",
        ),
        CheckConstraint(
            "(category = 'OAUTH' AND provider_code = 'GOOGLE' AND environment IS NULL) "
            "OR (category IN ('PROVIDER_CREDENTIAL', 'ACCOUNT_IDENTIFIER') "
            "AND provider_code IN ('KIS', 'TOSS', 'BINANCE') "
            "AND environment IN ('PAPER', 'LIVE'))",
            name="ck_backoffice_secret_version_scope",
        ),
        CheckConstraint(
            "OCTET_LENGTH(ciphertext) > 0 AND OCTET_LENGTH(ciphertext) <= 8192 "
            "AND OCTET_LENGTH(nonce) = 12 AND OCTET_LENGTH(fingerprint) = 32 "
            "AND version > 0 AND aad_schema_version = 1 AND master_key_version > 0",
            name="ck_backoffice_secret_version_crypto",
        ),
        Index("ix_backoffice_secret_version_history", "logical_name", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    logical_name: Mapped[str] = mapped_column(
        String(128, collation="utf8mb4_bin"), nullable=False
    )
    category: Mapped[str] = mapped_column(
        String(32, collation="ascii_bin"), nullable=False
    )
    provider_code: Mapped[str | None] = mapped_column(
        String(16, collation="ascii_bin"), nullable=True
    )
    environment: Mapped[str | None] = mapped_column(
        String(16, collation="ascii_bin"), nullable=True
    )
    version: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(VARBINARY(8192), nullable=False)
    nonce: Mapped[bytes] = mapped_column(BINARY(12), nullable=False)
    aad_schema_version: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    master_key_version: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    fingerprint: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class BackofficeSecretActivationRow(CoreBase):
    __tablename__ = "backoffice_secret_activation"
    __table_args__ = (
        UniqueConstraint(
            "secret_version_id", name="uq_backoffice_secret_activation_version"
        ),
        UniqueConstraint(
            "logical_name",
            "active_marker",
            name="uq_backoffice_secret_activation_active_name",
        ),
        UniqueConstraint(
            "id", "logical_name", name="uq_backoffice_secret_activation_exact_scope"
        ),
        CheckConstraint(
            "(deactivated_at IS NULL AND active_marker = 'ACTIVE') "
            "OR (deactivated_at > activated_at AND active_marker IS NULL)",
            name="ck_backoffice_secret_activation_state",
        ),
        ForeignKeyConstraint(
            ["secret_version_id", "logical_name"],
            ["backoffice_secret_version.id", "backoffice_secret_version.logical_name"],
            name="fk_backoffice_secret_activation_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["previous_activation_id", "logical_name"],
            [
                "backoffice_secret_activation.id",
                "backoffice_secret_activation.logical_name",
            ],
            name="fk_backoffice_secret_activation_previous",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_backoffice_secret_activation_history", "logical_name", "activated_at"
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    logical_name: Mapped[str] = mapped_column(
        String(128, collation="utf8mb4_bin"), nullable=False
    )
    secret_version_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    previous_activation_id: Mapped[UUID | None] = mapped_column(
        UuidBinary(), nullable=True
    )
    activated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    active_marker: Mapped[str | None] = mapped_column(
        String(6, collation="ascii_bin"), nullable=True
    )


class BackofficeSecondPasswordVersionRow(CoreBase):
    __tablename__ = "backoffice_second_password_version"
    __table_args__ = (
        UniqueConstraint("version", name="uq_backoffice_second_password_version"),
        UniqueConstraint("active_marker", name="uq_backoffice_second_password_active"),
        CheckConstraint(
            "version > 0 AND verifier LIKE '$argon2id$%' "
            "AND CHAR_LENGTH(verifier) > 0 AND verifier = TRIM(verifier)",
            name="ck_backoffice_second_password_verifier",
        ),
        CheckConstraint(
            "(retired_at IS NULL AND active_marker = 'ACTIVE') "
            "OR (retired_at > created_at AND active_marker IS NULL)",
            name="ck_backoffice_second_password_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    version: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    verifier: Mapped[str] = mapped_column(
        String(512, collation="utf8mb4_bin"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    active_marker: Mapped[str | None] = mapped_column(
        String(6, collation="ascii_bin"), nullable=True
    )


class BackofficeBootstrapAuthorityRow(CoreBase):
    __tablename__ = "backoffice_bootstrap_authority"
    __table_args__ = (
        UniqueConstraint("scope_key", name="uq_backoffice_bootstrap_scope"),
        CheckConstraint(
            "scope_key = 'PRIMARY' "
            "AND oauth_client_id_secret_id <> oauth_client_secret_secret_id "
            "AND OCTET_LENGTH(bootstrap_digest) = 32",
            name="ck_backoffice_bootstrap_scope",
        ),
        ForeignKeyConstraint(
            ["oauth_client_id_secret_id"],
            ["backoffice_secret_version.id"],
            name="fk_backoffice_bootstrap_oauth_client_id",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["oauth_client_secret_secret_id"],
            ["backoffice_secret_version.id"],
            name="fk_backoffice_bootstrap_oauth_client_secret",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["second_password_version_id"],
            ["backoffice_second_password_version.id"],
            name="fk_backoffice_bootstrap_second_password",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    scope_key: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    oauth_client_id_secret_id: Mapped[UUID] = mapped_column(
        UuidBinary(), nullable=False
    )
    oauth_client_secret_secret_id: Mapped[UUID] = mapped_column(
        UuidBinary(), nullable=False
    )
    second_password_version_id: Mapped[UUID] = mapped_column(
        UuidBinary(), nullable=False
    )
    bootstrap_digest: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class BackofficeCommandRow(CoreBase):
    __tablename__ = "backoffice_command"
    __table_args__ = (
        # This used to pin one address here. Who the operator is now comes
        # from BACKOFFICE_ALLOWED_EMAIL, which the identity gate already
        # refuses to start without and checks every session against, so the
        # literal was a second copy of a fact configuration owns. What the
        # schema still guarantees is the shape: an address, an address that
        # is trimmed, and every other column present.
        CheckConstraint(
            # `col = TRIM(col)` alone does not catch a value that is only
            # whitespace: the column collation pads spaces, so ' ' equals ''
            # and the comparison holds. Every column here had that gap;
            # actor_email's was covered by the pinned-address clause until
            # that was removed. CHAR_LENGTH(TRIM(col)) closes it for all six.
            "CHAR_LENGTH(TRIM(actor_email)) > 0 AND actor_email = TRIM(actor_email) "
            "AND CHAR_LENGTH(TRIM(source_ip)) > 0 AND source_ip = TRIM(source_ip) "
            "AND CHAR_LENGTH(TRIM(action)) > 0 AND action = TRIM(action) "
            "AND CHAR_LENGTH(TRIM(target_type)) > 0 "
            "AND target_type = TRIM(target_type) "
            "AND CHAR_LENGTH(TRIM(target_key)) > 0 AND target_key = TRIM(target_key) "
            "AND CHAR_LENGTH(TRIM(status)) > 0 AND status = TRIM(status)",
            name="ck_backoffice_command_scope",
        ),
        CheckConstraint(
            "OCTET_LENGTH(payload_digest) = 32 "
            "AND (expected_digest IS NULL OR OCTET_LENGTH(expected_digest) = 32)",
            name="ck_backoffice_command_digests",
        ),
        CheckConstraint(
            "(status = 'IN_PROGRESS' AND result_code IS NULL "
            "AND result_digest IS NULL AND completed_at IS NULL) "
            "OR (status IN ('SUCCEEDED', 'FAILED') "
            "AND CHAR_LENGTH(result_code) > 0 AND result_code = TRIM(result_code) "
            "AND OCTET_LENGTH(result_digest) = 32 AND completed_at >= started_at)",
            name="ck_backoffice_command_state",
        ),
        Index("ix_backoffice_command_actor_started", "actor_email", "started_at"),
        Index(
            "ix_backoffice_command_target_started",
            "target_type",
            "target_key",
            "started_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True, default=new_uuid7)
    actor_email: Mapped[str] = mapped_column(
        String(254, collation="ascii_bin"), nullable=False
    )
    source_ip: Mapped[str] = mapped_column(
        String(45, collation="ascii_bin"), nullable=False
    )
    action: Mapped[str] = mapped_column(
        String(64, collation="ascii_bin"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(
        String(64, collation="ascii_bin"), nullable=False
    )
    target_key: Mapped[str] = mapped_column(
        String(512, collation="utf8mb4_bin"), nullable=False
    )
    payload_digest: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    expected_digest: Mapped[bytes | None] = mapped_column(BINARY(32), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16, collation="ascii_bin"), nullable=False
    )
    result_code: Mapped[str | None] = mapped_column(
        String(128, collation="ascii_bin"), nullable=True
    )
    result_digest: Mapped[bytes | None] = mapped_column(BINARY(32), nullable=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


__all__ = (
    "BackofficeBootstrapAuthorityRow",
    "BackofficeCommandRow",
    "BackofficeSecondPasswordVersionRow",
    "BackofficeSecretActivationRow",
    "BackofficeSecretVersionRow",
)
