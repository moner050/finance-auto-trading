"""Which accounts may be deleted, and which may not.

An account row is cheap to create and there was no way to take one back, so
the screen accumulates typos and abandoned attempts.

Deleting a used one is a different act: the orders, fills, positions and
promotion sessions pointing at it are the record of what this system did with
somebody's money. The rule is not "delete carefully" but "delete only what
nothing refers to", and these are the checks that make that true rather than
the operator's memory.
"""

from __future__ import annotations

import importlib
import pkgutil

import autotrader.persistence.mysql.models as models
from autotrader.apps.backoffice.account_removal import referencing_columns

for _info in pkgutil.iter_modules(models.__path__):
    importlib.import_module(f"autotrader.persistence.mysql.models.{_info.name}")


def test_the_tables_to_check_come_from_the_metadata() -> None:
    """Listing them here would be correct until the next table is added, and
    a stale list fails as a delete that succeeds and leaves rows pointing at
    nothing."""
    columns = referencing_columns()

    assert columns
    # Every one of them really does carry a foreign key onto the account.
    from autotrader.persistence.mysql.models.accounts import Account

    tables = Account.metadata.tables
    for table, column in columns:
        keys = {
            key.parent.name
            for key in tables[table].foreign_keys
            if key.column.table.name == "exec_account" and key.column.name == "id"
        }
        assert column in keys, f"{table}.{column}"


def test_the_tables_that_hold_the_trading_record_are_among_them() -> None:
    """Named so that a change removing one of them from the check fails here
    rather than in production, where it would look like a successful
    delete."""
    tables = {table for table, _ in referencing_columns()}

    for required in (
        "exec_order",
        "exec_fill",
        "exec_position",
        "exec_account_risk_policy_binding",
        "exec_provider_account_binding",
    ):
        assert required in tables


def test_no_column_is_checked_twice() -> None:
    columns = referencing_columns()

    assert len(columns) == len(set(columns))
