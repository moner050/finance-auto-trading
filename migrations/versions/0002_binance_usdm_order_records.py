"""The Binance USD-M durable order records.

0001 mirrors the ORM metadata, so a database created from scratch already has
these two tables by the time this runs. A database stamped at 0001 before they
existed does not, and would never get them - which is the gap this closes.

Both paths therefore reach the same schema, and each statement is skipped when
what it would create is already there rather than being made conditional in
SQL, so the check is one the migration can explain.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_binance_usdm_order_records"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = (
    "binance_usdm_normal_order",
    "binance_usdm_algo_order",
)

_CREATE: tuple[tuple[str, str], ...] = (
    (
        "binance_usdm_normal_order",
        """CREATE TABLE binance_usdm_normal_order (
    command_id BINARY(16) NOT NULL,
    binding_id BINARY(16) NOT NULL,
    account_id BINARY(16) NOT NULL,
    client_order_id VARCHAR(36) COLLATE ascii_bin NOT NULL,
    request_body VARBINARY(2048) NOT NULL,
    request_digest VARBINARY(32) NOT NULL,
    prepared_at DATETIME(6) NOT NULL,
    not_after DATETIME(6) NOT NULL,
    dispatch_count BIGINT NOT NULL,
    state VARCHAR(16) COLLATE ascii_bin NOT NULL,
    result JSON,
    PRIMARY KEY (command_id),
    CONSTRAINT uq_binance_usdm_normal_order_client_id UNIQUE (client_order_id),
    CONSTRAINT ck_binance_usdm_normal_order_state CHECK (state IN ('PREPARED', 'NOT_SENT', 'AMBIGUOUS', 'ACKNOWLEDGED', 'REJECTED', 'UNKNOWN')),
    CONSTRAINT ck_binance_usdm_normal_order_values CHECK (OCTET_LENGTH(request_digest) = 32 AND OCTET_LENGTH(request_body) > 0 AND CHAR_LENGTH(TRIM(client_order_id)) > 0 AND dispatch_count >= 1 AND prepared_at < not_after),
    CONSTRAINT ck_binance_usdm_normal_order_result CHECK ((state = 'ACKNOWLEDGED' AND result IS NOT NULL) OR (state <> 'ACKNOWLEDGED' AND result IS NULL)),
    CONSTRAINT fk_binance_usdm_normal_order_binding FOREIGN KEY(binding_id, account_id) REFERENCES exec_provider_account_binding (id, account_id) ON DELETE RESTRICT
)""",
    ),
    (
        "binance_usdm_algo_order",
        """CREATE TABLE binance_usdm_algo_order (
    placement_command_id BINARY(16) NOT NULL,
    entry_command_id BINARY(16) NOT NULL,
    client_algo_id VARCHAR(36) COLLATE ascii_bin NOT NULL,
    binding_id BINARY(16) NOT NULL,
    account_id BINARY(16) NOT NULL,
    instrument_id BINARY(16) NOT NULL,
    emergency_close_command_id BINARY(16) NOT NULL,
    side VARCHAR(4) COLLATE ascii_bin NOT NULL,
    symbol VARCHAR(16) COLLATE ascii_bin NOT NULL,
    first_fill_quantity NUMERIC(38, 18) NOT NULL,
    cumulative_quantity_before NUMERIC(38, 18) NOT NULL,
    average_fill_price NUMERIC(38, 18) NOT NULL,
    tick_size NUMERIC(38, 18) NOT NULL,
    trigger_price NUMERIC(38, 18) NOT NULL,
    filled_at DATETIME(6) NOT NULL,
    protection_deadline DATETIME(6) NOT NULL,
    prepared_at DATETIME(6) NOT NULL,
    request_body VARBINARY(2048) NOT NULL,
    request_digest VARBINARY(32) NOT NULL,
    state VARCHAR(20) COLLATE ascii_bin NOT NULL,
    result JSON,
    PRIMARY KEY (placement_command_id),
    CONSTRAINT uq_binance_usdm_algo_order_client_id UNIQUE (client_algo_id),
    CONSTRAINT ck_binance_usdm_algo_order_state CHECK (state IN ('PREPARED', 'AMBIGUOUS', 'ACTIVE', 'REJECTED', 'EMERGENCY_CLOSED', 'UNKNOWN', 'SUPERSEDED')),
    CONSTRAINT ck_binance_usdm_algo_order_scope CHECK (side IN ('BUY', 'SELL') AND symbol = 'BTCUSDT'),
    CONSTRAINT ck_binance_usdm_algo_order_values CHECK (OCTET_LENGTH(request_digest) = 32 AND OCTET_LENGTH(request_body) > 0 AND CHAR_LENGTH(TRIM(client_algo_id)) > 0 AND first_fill_quantity > 0 AND cumulative_quantity_before >= 0 AND average_fill_price > 0 AND tick_size > 0 AND trigger_price > 0),
    CONSTRAINT ck_binance_usdm_algo_order_window CHECK (filled_at < protection_deadline AND prepared_at >= filled_at),
    CONSTRAINT ck_binance_usdm_algo_order_result CHECK ((state IN ('ACTIVE', 'EMERGENCY_CLOSED') AND result IS NOT NULL) OR (state NOT IN ('ACTIVE', 'EMERGENCY_CLOSED') AND result IS NULL)),
    CONSTRAINT fk_binance_usdm_algo_order_binding FOREIGN KEY(binding_id, account_id) REFERENCES exec_provider_account_binding (id, account_id) ON DELETE RESTRICT
)""",
    ),
)

_INDEX: tuple[tuple[str, str, str], ...] = (
    (
        "ix_binance_usdm_normal_order_unresolved",
        "binance_usdm_normal_order",
        """CREATE INDEX ix_binance_usdm_normal_order_unresolved ON binance_usdm_normal_order (binding_id, state, not_after)""",
    ),
    (
        "ix_binance_usdm_algo_order_unsafe",
        "binance_usdm_algo_order",
        """CREATE INDEX ix_binance_usdm_algo_order_unsafe ON binance_usdm_algo_order (binding_id, state, protection_deadline)""",
    ),
    (
        "ix_binance_usdm_algo_order_entry",
        "binance_usdm_algo_order",
        """CREATE INDEX ix_binance_usdm_algo_order_entry ON binance_usdm_algo_order (entry_command_id, prepared_at)""",
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    present = set(inspector.get_table_names())
    for name, statement in _CREATE:
        if name in present:
            continue
        op.execute(sa.text(statement))
    for name, table, statement in _INDEX:
        if table in present and name in {
            index["name"] for index in inspector.get_indexes(table)
        }:
            continue
        op.execute(sa.text(statement))


def downgrade() -> None:
    for name in reversed(TABLES):
        op.execute(sa.text(f"DROP TABLE IF EXISTS {name}"))
