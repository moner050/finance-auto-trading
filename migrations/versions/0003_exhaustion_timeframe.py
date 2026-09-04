"""Which series confirmed exhaustion, on the decision row.

Section 4.2 (A0) confirms exhaustion at thirty seconds and the five-minute
macro series is the fallback, so a decision is now taken on one of two scales.
Nothing recorded said which: the row keeps a bare list of evidence digests
with no keys, `completed_evidence_at` is the decision moment rather than the
evidence's, and the feature snapshot keeps a hash without its payload. The
plan's section 33.17 found that out by trying to read it back.

The digest does change with the scale, which keeps two bundles from colliding.
It does not tell a reader which one this was, and section 33.11 measured a
132-to-1 difference between the two.

Nullable, and existing rows are left alone. Every decision written before this
column existed was taken on five minutes, but writing that in would be putting
a value where the system had no answer - and the whole point of the column is
to be able to trust what it says.

Like 0002, this follows a database created from scratch by 0001 (which mirrors
the ORM metadata and therefore already has the column) as well as one stamped
before it existed, so the statement is skipped when the column is already
there rather than being made conditional in SQL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Kept under 32 characters because that is what `alembic_version.version_num`
# holds. The first spelling of this was 34, so the DDL applied - MySQL
# commits it - and then the stamp failed, leaving the schema ahead of the
# revision it claimed. `test_a_revision_id_fits_the_version_table` is there
# so the next one fails in the suite instead of against the database.
revision: str = "0003_exhaustion_timeframe"
down_revision: str | Sequence[str] | None = "0002_binance_usdm_order_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "strategy_david_v6_decision"
COLUMN = "exhaustion_timeframe"
CONSTRAINT = "ck_strategy_david_v6_decision_exhaustion_timeframe"

_ADD = f"""ALTER TABLE {TABLE}
    ADD COLUMN {COLUMN} VARCHAR(8) NULL,
    ADD CONSTRAINT {CONSTRAINT} CHECK (
        {COLUMN} IS NULL
        OR {COLUMN} IN ('5s', '30s', '1m', '5m', '15m', '1h', '1d')
    )"""


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in set(inspector.get_table_names()):
        return
    if COLUMN in {column["name"] for column in inspector.get_columns(TABLE)}:
        return
    op.execute(sa.text(_ADD))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in set(inspector.get_table_names()):
        return
    if COLUMN not in {column["name"] for column in inspector.get_columns(TABLE)}:
        return
    op.execute(sa.text(f"ALTER TABLE {TABLE} DROP CHECK {CONSTRAINT}"))
    op.execute(sa.text(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}"))
