from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.binance_usdm import (
    BinanceUsdmAlgoOrderFactRow,
    BinanceUsdmBalanceFactRow,
    BinanceUsdmConfigurationFactRow,
    BinanceUsdmIncomeFactRow,
    BinanceUsdmNormalOrderRow,
    BinanceUsdmOrderFactRow,
    BinanceUsdmPositionFactRow,
    BinanceUsdmReconciliationRunRow,
    BinanceUsdmTradeFactRow,
)


class BinanceUsdmReconciliationRepository:
    """Appends one immutable Binance USD-M reconciliation bundle."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def persist_bundle(
        self,
        *,
        run: BinanceUsdmReconciliationRunRow,
        balances: Sequence[BinanceUsdmBalanceFactRow],
        positions: Sequence[BinanceUsdmPositionFactRow],
        normal_orders: Sequence[BinanceUsdmOrderFactRow],
        algo_orders: Sequence[BinanceUsdmAlgoOrderFactRow],
        trades: Sequence[BinanceUsdmTradeFactRow],
        income: Sequence[BinanceUsdmIncomeFactRow],
        configurations: Sequence[BinanceUsdmConfigurationFactRow],
    ) -> BinanceUsdmReconciliationRunRow:
        if type(run) is not BinanceUsdmReconciliationRunRow:
            raise TypeError("run must be an exact Binance reconciliation row")
        groups = (
            _exact_rows(balances, BinanceUsdmBalanceFactRow, "balances"),
            _exact_rows(positions, BinanceUsdmPositionFactRow, "positions"),
            _exact_rows(normal_orders, BinanceUsdmOrderFactRow, "normal_orders"),
            _exact_rows(algo_orders, BinanceUsdmAlgoOrderFactRow, "algo_orders"),
            _exact_rows(trades, BinanceUsdmTradeFactRow, "trades"),
            _exact_rows(income, BinanceUsdmIncomeFactRow, "income"),
            _exact_rows(
                configurations,
                BinanceUsdmConfigurationFactRow,
                "configurations",
            ),
        )
        _validate_bundle(run, groups)
        existing = await self._session.scalar(
            select(BinanceUsdmReconciliationRunRow.id).where(
                BinanceUsdmReconciliationRunRow.id == run.id
            )
        )
        if existing is not None:
            raise ValueError("Binance USD-M reconciliation is append-only")
        self._session.add(run)
        self._session.add_all([row for group in groups for row in group])
        await self._session.flush()
        return run

    async def update_completed_run(self, run: BinanceUsdmReconciliationRunRow) -> None:
        del run
        raise ValueError("Binance USD-M reconciliation is append-only")

    async def delete_completed_run(self, run_id: UUID) -> None:
        del run_id
        raise ValueError("Binance USD-M reconciliation is append-only")


def _exact_rows[RowT](
    rows: Sequence[RowT],
    row_type: type[RowT],
    name: str,
) -> tuple[RowT, ...]:
    if isinstance(rows, (str, bytes)):
        raise TypeError(f"{name} must be a row sequence")
    result = tuple(rows)
    if any(type(row) is not row_type for row in result):
        raise TypeError(f"{name} contains an invalid row")
    return result


def _validate_bundle(
    run: BinanceUsdmReconciliationRunRow,
    groups: tuple[tuple[object, ...], ...],
) -> None:
    if (
        run.provider_code != "BINANCE"
        or run.market_code != "USD-M"
        or run.symbol != "BTCUSDT"
        or run.settlement_asset != "USDT"
    ):
        raise ValueError("reconciliation run is outside Binance USD-M scope")
    if type(run.fact_digest) is not bytes or len(run.fact_digest) != 32:
        raise ValueError("fact_digest must be SHA-256 bytes")
    expected_counts = (
        run.balance_fact_count,
        run.position_fact_count,
        run.order_fact_count,
        run.algo_order_fact_count,
        run.trade_fact_count,
        run.income_fact_count,
        run.configuration_fact_count,
    )
    if expected_counts != tuple(len(group) for group in groups):
        raise ValueError("Binance USD-M fact counts do not match the bundle")
    for group in groups:
        for row in group:
            if getattr(row, "run_id", None) != run.id:
                raise ValueError("Binance USD-M fact has the wrong run identity")
    passing = (
        len(groups[0]) >= 1
        and len(groups[1]) >= 1
        and len(groups[6]) == 1
        and not run.blockers
    )
    expected_result = "COMPLETE" if passing else "PARTIAL"
    if run.result != expected_result:
        raise ValueError("Binance USD-M blockers make reconciliation non-passing")


__all__ = ("BinanceUsdmReconciliationRepository",)


class BinanceUsdmNormalOrderRepository:
    """Row access for the durable order record the order service claims."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_if_absent(self, values: dict[str, object]) -> bool:
        """True when this caller wrote the row rather than finding it.

        `INSERT IGNORE` makes the claim one statement, so two processes
        preparing the same client order id cannot both believe they own it.
        A claim decided by a read followed by a write would have a window
        between them, and that window is a duplicate order.
        """
        result = await self._session.execute(
            insert(BinanceUsdmNormalOrderRow).values(**values).prefix_with("IGNORE")
        )
        return result.rowcount == 1

    async def load(
        self, client_order_id: str, *, lock: bool = False
    ) -> BinanceUsdmNormalOrderRow | None:
        statement = select(BinanceUsdmNormalOrderRow).where(
            BinanceUsdmNormalOrderRow.client_order_id == client_order_id
        )
        if lock:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def apply(self, client_order_id: str, values: dict[str, object]) -> int:
        result = await self._session.execute(
            update(BinanceUsdmNormalOrderRow)
            .where(BinanceUsdmNormalOrderRow.client_order_id == client_order_id)
            .values(**values)
        )
        return result.rowcount


__all__ = (
    "BinanceUsdmNormalOrderRepository",
    "BinanceUsdmReconciliationRepository",
)
