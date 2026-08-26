"""Emit the consolidated initial migration from the ORM metadata.

Hand transcribing seventy-odd tables invites drift between the models and the
schema, which is exactly what went unnoticed before. Generate the DDL from the
metadata instead, and let tests/integration/migrations check that the committed
file still matches.

Usage: python scripts/generate-initial-migration.py [--check]
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

from sqlalchemy import Table
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from autotrader.persistence.mysql.models import metadata

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "migrations" / "versions" / "0001_initial.py"
REVISION = "0001_initial"

_HEADER = '''"""Consolidated initial schema.

Generated from the ORM metadata by scripts/generate-initial-migration.py.
Do not edit by hand: regenerate it, and add later changes as new migrations.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "{revision}"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = (
{tables}
)

_CREATE: tuple[str, ...] = (
{statements}
)


def upgrade() -> None:
    # exec_order, exec_order_intent, exec_reconciliation_diff and risk_decision
    # reference each other, so no creation order resolves every foreign key.
    op.execute("SET FOREIGN_KEY_CHECKS = 0")
    try:
        for statement in _CREATE:
            op.execute(statement)
    finally:
        op.execute("SET FOREIGN_KEY_CHECKS = 1")


def downgrade() -> None:
    op.execute("SET FOREIGN_KEY_CHECKS = 0")
    for name in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS `{{name}}`")
    op.execute("SET FOREIGN_KEY_CHECKS = 1")
'''


def _ordered_tables() -> list[Table]:
    """Parents first where possible; cycles are handled by the migration."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Cannot correctly sort tables")
        return list(metadata.sorted_tables)


def _statements(tables: list[Table]) -> list[str]:
    dialect = mysql.dialect()
    statements: list[str] = []
    for table in tables:
        statements.append(_clean(CreateTable(table).compile(dialect=dialect)))
    for table in tables:
        for index in sorted(table.indexes, key=lambda value: value.name or ""):
            statements.append(_clean(CreateIndex(index).compile(dialect=dialect)))
    return statements


def _clean(compiled: object) -> str:
    """Drop the compiler's trailing spaces and tabs so the file stays tidy."""
    lines = str(compiled).strip().splitlines()
    return "\n".join(line.replace("\t", "    ").rstrip() for line in lines)


def render() -> str:
    tables = _ordered_tables()
    table_lines = "\n".join(f'    "{table.name}",' for table in tables)
    statement_lines = "\n".join(
        f'    """{statement}""",' for statement in _statements(tables)
    )
    return _HEADER.format(
        revision=REVISION,
        tables=table_lines,
        statements=statement_lines,
    )


def main() -> int:
    rendered = render()
    if "--check" in sys.argv:
        if not TARGET.exists():
            print(f"MISSING {TARGET}")
            return 1
        if TARGET.read_text(encoding="utf-8") != rendered:
            print("INITIAL_MIGRATION_STALE=1")
            return 1
        print("INITIAL_MIGRATION_CURRENT=1")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(rendered, encoding="utf-8")
    print(f"WROTE {TARGET.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
