"""Durable journal for the internal paper broker.

The broker stages a command when it is sent and writes a receipt when the bar
that resolves it closes. Keeping both in MySQL is what lets a restart tell a
paper order that was never filled from one that was filled and forgotten.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.domain.enums import OrderStyle, Side
from autotrader.integrations.brokers.internal_paper import (
    PaperOrderCommand,
    PaperOrderReceipt,
    PaperOrderStatus,
)
from autotrader.persistence.mysql.models.paper import PaperOrderRow
from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.models import V6Market


class MySqlPaperJournal:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_receipt(self, command_id: object) -> PaperOrderReceipt | None:
        row = await self._row(command_id)
        if row is None or row.status is None:
            return None
        return _to_receipt(row)

    async def stage_command(self, command: PaperOrderCommand, digest: bytes) -> None:
        if type(command) is not PaperOrderCommand:
            raise TypeError("command must be an exact PaperOrderCommand")
        if type(digest) is not bytes or len(digest) != 32:
            raise ValueError("digest must be SHA-256 bytes")
        existing = await self._row(command.id)
        if existing is not None:
            if existing.command_digest != digest:
                raise ValueError("paper command identity payload collision")
            return
        self._session.add(
            PaperOrderRow(
                command_id=command.id,
                order_id=command.order_id,
                account_alias=command.account_alias,
                market=command.market.value,
                side=command.side.value,
                order_style=command.order_style.value,
                quantity=command.quantity,
                limit_price=command.limit_price,
                trigger_price=command.trigger_price,
                signal_at=command.signal_at,
                timeframe_seconds=int(command.timeframe.total_seconds()),
                fee_per_unit=command.fee_per_unit,
                slippage_per_unit=command.slippage_per_unit,
                command_digest=digest,
                staged_at=command.signal_at,
            )
        )
        await self._session.flush()

    async def persist_receipt(self, receipt: PaperOrderReceipt) -> None:
        if type(receipt) is not PaperOrderReceipt:
            raise TypeError("receipt must be an exact PaperOrderReceipt")
        row = await self._row(receipt.command_id)
        if row is None:
            raise ValueError("a receipt requires the staged paper command")
        if row.command_digest != receipt.command_digest:
            raise ValueError("paper receipt does not match the staged command")
        if row.status is not None:
            # A resolved paper order is settled: re-running the fill must not
            # move it, or a replay would rewrite history.
            return
        row.status = receipt.status.value
        row.filled_quantity = receipt.filled_quantity
        row.remaining_quantity = receipt.remaining_quantity
        row.fill_price = receipt.fill_price
        row.fee = receipt.fee
        row.slippage_cost = receipt.slippage_cost
        row.filled_at = receipt.filled_at
        row.reason_code = receipt.reason_code
        row.source_digest = receipt.source_digest
        await self._session.flush()

    async def latest_receipt_for_order(
        self, order_id: object
    ) -> PaperOrderReceipt | None:
        if not isinstance(order_id, UUID):
            raise TypeError("order_id must be a UUID")
        row = await self._session.scalar(
            select(PaperOrderRow)
            .where(
                PaperOrderRow.order_id == order_id,
                PaperOrderRow.status.is_not(None),
            )
            .order_by(PaperOrderRow.command_id.desc())
            .limit(1)
        )
        return None if row is None else _to_receipt(row)

    async def staged_command(self, command_id: UUID) -> PaperOrderCommand | None:
        """The command as sent, for resolving a fill on a later tick."""
        row = await self._row(command_id)
        return None if row is None else _to_command(row)

    async def unresolved_commands(
        self, *, order_id: UUID | None = None
    ) -> tuple[PaperOrderCommand, ...]:
        statement = select(PaperOrderRow).where(PaperOrderRow.status.is_(None))
        if order_id is not None:
            statement = statement.where(PaperOrderRow.order_id == order_id)
        rows = (await self._session.scalars(statement)).all()
        return tuple(_to_command(row) for row in rows)

    async def _row(self, command_id: object) -> PaperOrderRow | None:
        if not isinstance(command_id, UUID):
            raise TypeError("command_id must be a UUID")
        return await self._session.get(PaperOrderRow, command_id)


def _to_command(row: PaperOrderRow) -> PaperOrderCommand:
    return PaperOrderCommand(
        id=row.command_id,
        order_id=row.order_id,
        account_alias=row.account_alias,
        market=V6Market(row.market),
        side=Side(row.side),
        order_style=OrderStyle(row.order_style),
        quantity=row.quantity,
        limit_price=row.limit_price,
        trigger_price=row.trigger_price,
        signal_at=require_utc(row.signal_at),
        timeframe=timedelta(seconds=row.timeframe_seconds),
        fee_per_unit=row.fee_per_unit,
        slippage_per_unit=row.slippage_per_unit,
    )


def _to_receipt(row: PaperOrderRow) -> PaperOrderReceipt:
    assert row.status is not None
    return PaperOrderReceipt(
        command_id=row.command_id,
        order_id=row.order_id,
        status=PaperOrderStatus(row.status),
        requested_quantity=row.quantity,
        filled_quantity=cast(Decimal, row.filled_quantity),
        remaining_quantity=cast(Decimal, row.remaining_quantity),
        fill_price=row.fill_price,
        fee=cast(Decimal, row.fee),
        slippage_cost=cast(Decimal, row.slippage_cost),
        filled_at=(require_utc(row.filled_at) if row.filled_at is not None else None),
        reason_code=row.reason_code,
        source_digest=row.source_digest,
        command_digest=row.command_digest,
    )


__all__ = ("MySqlPaperJournal",)
