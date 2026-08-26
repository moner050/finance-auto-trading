"""Offline guards on the consolidated initial migration.

Applying it needs a MySQL server, which these tests do not have. What they can
prove without one is that the committed file still matches the ORM metadata,
that it creates every table exactly once, and that it creates them in an order
a foreign key can resolve.
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


def test_it_is_the_only_revision() -> None:
    versions = sorted(
        path.name
        for path in (ROOT / "migrations" / "versions").glob("*.py")
        if path.name != "__init__.py"
    )

    assert versions == ["0001_initial.py"]


def test_a_table_is_created_before_anything_references_it() -> None:
    module = _module()
    created: list[str] = []
    for name in module.TABLES:
        table = metadata.tables[name]
        for key in sorted(
            foreign_key._table_key() for foreign_key in table.foreign_keys
        ):
            # A self reference resolves within the same statement.
            if key == name:
                continue
            assert key in created, f"{name} references {key} before it exists"
        created.append(name)


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
