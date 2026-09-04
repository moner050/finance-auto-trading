"""Offline guards on the consolidated initial migration.

Applying it needs a MySQL server, which these tests do not have. What they can
prove without one is that the committed file still matches the ORM metadata,
that it creates every table exactly once, that it creates them in an order a
foreign key can resolve, and that whatever follows it forms one chain.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from autotrader.persistence.mysql.models import metadata

ROOT = Path(__file__).resolve().parents[4]
MIGRATION = ROOT / "migrations" / "versions" / "0001_initial.py"
GENERATOR = ROOT / "scripts" / "generate-initial-migration.py"


def _load(name: str, path: Path) -> object:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _module() -> object:
    return _load("initial_migration", MIGRATION)


def test_the_migration_is_committed() -> None:
    assert MIGRATION.is_file()


def test_the_migration_still_matches_the_orm_metadata() -> None:
    """The drift the old chain never detected, caught without a database."""
    generator = _load("initial_migration_generator", GENERATOR)
    render = cast("Callable[[], str]", generator.render)

    assert MIGRATION.read_text(encoding="utf-8") == render(), (
        "0001_initial.py is stale; run python scripts/generate-initial-migration.py"
    )


def test_it_creates_every_mapped_table_exactly_once() -> None:
    module = _module()
    declared = tuple(module.TABLES)

    assert set(declared) == set(metadata.tables)
    assert len(declared) == len(set(declared))


def test_it_is_the_base_revision() -> None:
    module = _module()

    assert module.revision == "0001_initial"
    assert module.down_revision is None


def test_the_revisions_form_one_chain_from_it() -> None:
    """This used to require 0001 to be the only file.

    It could, while nothing had been deployed: the consolidation existed to
    end a chain whose drift went undetected, and regenerating one file is the
    simplest way to keep the schema and the metadata together.

    A database is now stamped at 0001, so regenerating it no longer reaches
    that database at all - alembic considers the revision applied. Later
    changes have to arrive as later revisions.

    What the consolidation was protecting is untouched:
    `test_the_migration_still_matches_the_orm_metadata` and the
    `--check` gate both still hold 0001 to the metadata. What is admitted
    here is only that deltas may follow it, in one line, with one head.
    """
    versions = sorted(
        path
        for path in (ROOT / "migrations" / "versions").glob("*.py")
        if path.name != "__init__.py"
    )
    assert versions[0].name == "0001_initial.py"

    links: dict[str, str | None] = {}
    for index, path in enumerate(versions):
        module = _load(f"migration_{index}", path)
        revision = cast("str", module.revision)
        assert revision not in links, f"{revision} is declared twice"
        links[revision] = cast("str | None", module.down_revision)

    bases = [name for name, parent in links.items() if parent is None]
    assert bases == ["0001_initial"]

    parents = {parent for parent in links.values() if parent is not None}
    assert parents <= set(links), "a revision names a parent that is not here"
    heads = [name for name in links if name not in parents]
    assert len(heads) == 1, f"the chain has forked: {sorted(heads)}"


def test_creation_defers_foreign_keys_because_the_schema_has_cycles() -> None:
    """exec_order and its intent, diff and decision reference each other.

    No creation order resolves every foreign key, so the migration disables the
    checks while creating and restores them afterwards, as a dump would.
    """
    source = MIGRATION.read_text(encoding="utf-8")
    upgrade = source[source.index("def upgrade()") : source.index("def downgrade()")]

    assert 'op.execute("SET FOREIGN_KEY_CHECKS = 0")' in upgrade
    assert 'op.execute("SET FOREIGN_KEY_CHECKS = 1")' in upgrade
    assert "finally:" in upgrade


def test_downgrade_drops_every_table_it_created() -> None:
    module = _module()
    source = MIGRATION.read_text(encoding="utf-8")
    downgrade = source[source.index("def downgrade()") :]

    assert "DROP TABLE IF EXISTS" in downgrade
    assert 'op.execute("SET FOREIGN_KEY_CHECKS = 0")' in downgrade
    assert set(module.TABLES) == set(metadata.tables)


def test_no_foreign_key_points_at_a_table_that_no_longer_exists() -> None:
    dangling = {
        f"{table.name}.{foreign_key.parent.name} -> {foreign_key._table_key()}"
        for table in metadata.tables.values()
        for foreign_key in table.foreign_keys
        if foreign_key._table_key() not in metadata.tables
    }

    assert dangling == set()


@pytest.mark.parametrize(
    "table",
    (
        "exec_account",
        "exec_order",
        "exec_fill",
        "exec_position",
        "exec_provider_account_binding",
        "ops_trading_control",
        "strategy_david_v6_decision",
        "backoffice_secret_version",
    ),
)
def test_the_tables_the_loop_and_backoffice_need_are_present(table: str) -> None:
    assert table in metadata.tables


@pytest.mark.parametrize(
    "retired",
    (
        "strategy_shadow_candidate",
        "ops_gate_scenario_scope",
        "risk_limit",
        "binance_usdm_command_state",
    ),
)
def test_retired_tables_are_not_carried_into_the_new_schema(retired: str) -> None:
    assert retired not in metadata.tables


def test_a_revision_id_fits_the_version_table() -> None:
    """Alembic stores the revision in a VARCHAR(32).

    A longer one does not fail early: MySQL commits the DDL, and only the
    stamp that follows it is refused. The schema is then ahead of the revision
    the database claims to be at, which is the one state the whole revision
    chain exists to make impossible. It happened once, to 0003.
    """
    for path in (ROOT / "migrations" / "versions").glob("*.py"):
        if path.name == "__init__.py":
            continue
        module = _load(f"revision_{path.stem}", path)
        revision = module.revision
        assert isinstance(revision, str)
        assert len(revision) <= 32, f"{path.name}: {revision} is {len(revision)}"
