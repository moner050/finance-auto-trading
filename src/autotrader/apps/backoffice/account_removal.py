"""Delete an account that was never used, and refuse any other.

An account row is cheap to create and there is no way to take one back, so
the screen accumulates typos and abandoned attempts that an operator then has
to read past every time they look for the real one.

Deleting a used account is a different thing entirely: the orders, fills,
positions, reconciliations and promotion sessions that point at it are the
record of what this system did with somebody's money. So the rule is not
"delete carefully", it is "delete only what nothing refers to", and the check
is what makes that true rather than the operator's memory.

The tables to check are read out of the mapper metadata rather than listed.
Fourteen of them point at `exec_account.id` today. A list written here would
be correct until the next table is added, and the failure of a stale list is
a delete that succeeds and leaves rows pointing at nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import Label

from autotrader.persistence.mysql.models.accounts import Account

ACCOUNT_TABLE = "exec_account"
ACCOUNT_COLUMN = "id"


class AccountRemovalRefusedError(RuntimeError):
    """Raised when the account is not one that may be deleted."""


@dataclass(frozen=True, slots=True)
class Reference:
    """One table that still points at the account, and how many rows do."""

    table: str
    column: str
    rows: int


def referencing_columns() -> tuple[tuple[str, str], ...]:
    """Every (table, column) with a foreign key onto the account's id.

    Read from the metadata so a table added later is covered without anyone
    remembering this file exists.
    """
    found: list[tuple[str, str]] = []
    for table in Account.metadata.tables.values():
        for key in table.foreign_keys:
            if (
                key.column.table.name == ACCOUNT_TABLE
                and key.column.name == ACCOUNT_COLUMN
            ):
                found.append((table.name, key.parent.name))
    return tuple(sorted(set(found)))


async def references_to(
    session: AsyncSession, account_id: UUID
) -> tuple[Reference, ...]:
    """What still points at this account, counted in one round trip.

    One statement of scalar subqueries rather than fourteen counts: the
    database this runs against answers a trivial query in thirty
    milliseconds, and fourteen of those is most of a second for a question
    whose answer is usually zero.
    """
    columns = referencing_columns()
    if not columns:
        return ()
    tables = Account.metadata.tables
    statement = select(
        *[
            select(func.count())
            .select_from(tables[table])
            .where(tables[table].c[column] == account_id)
            .scalar_subquery()
            .label(f"c{index}")
            for index, (table, column) in enumerate(columns)
        ]
    )
    counts = (await session.execute(statement)).one()
    return tuple(
        Reference(table=table, column=column, rows=int(counts[index] or 0))
        for index, (table, column) in enumerate(columns)
        if (counts[index] or 0) > 0
    )


async def unreferenced_accounts(
    session: AsyncSession, account_ids: tuple[UUID, ...]
) -> frozenset[UUID]:
    """Which of these accounts nothing points at, in one round trip.

    The screen needs this to decide whether to offer deletion at all. A
    button that is always there and usually refuses teaches an operator to
    click it and read the error, which is the opposite of what a delete
    button should teach.

    Asked in bulk because the alternative is fourteen counts per account, and
    the page already waits on a database that is thirty milliseconds away.
    """
    if not account_ids:
        return frozenset()
    columns = referencing_columns()
    tables = Account.metadata.tables
    labels: list[Label[int]] = []
    for outer, account_id in enumerate(account_ids):
        for inner, (table, column) in enumerate(columns):
            labels.append(
                select(func.count())
                .select_from(tables[table])
                .where(tables[table].c[column] == account_id)
                .scalar_subquery()
                .label(f"c{outer}_{inner}")
            )
    counts = (await session.execute(select(*labels))).one()
    clean: list[UUID] = []
    width = len(columns)
    for outer, account_id in enumerate(account_ids):
        window = counts[outer * width : (outer + 1) * width]
        if not any(int(value or 0) for value in window):
            clean.append(account_id)
    return frozenset(clean)


async def delete_unused_account(session: AsyncSession, *, account_id: UUID) -> str:
    """Delete the account, or say what is still pointing at it.

    Returns the alias, so the screen can say what went rather than echoing an
    id the operator would have to look up.
    """
    account = await session.get(Account, account_id)
    if account is None:
        raise AccountRemovalRefusedError("그런 계정이 없습니다")
    if account.enabled:
        # An account permitted to trade is not an unused one, whatever the
        # tables say. Disabling it first is a decision someone makes on
        # purpose; deleting it is not the place to make that decision
        # implicitly.
        raise AccountRemovalRefusedError(
            "활성화된 계정은 삭제할 수 없습니다. 먼저 비활성화하십시오."
        )
    found = await references_to(session, account_id)
    if found:
        detail = ", ".join(f"{item.table} {item.rows}건" for item in found)
        raise AccountRemovalRefusedError(
            f"이 계정을 참조하는 기록이 있어 삭제할 수 없습니다: {detail}"
        )
    alias = account.account_alias
    await session.delete(account)
    await session.flush()
    return alias


__all__ = (
    "AccountRemovalRefusedError",
    "Reference",
    "delete_unused_account",
    "references_to",
    "referencing_columns",
    "unreferenced_accounts",
)
