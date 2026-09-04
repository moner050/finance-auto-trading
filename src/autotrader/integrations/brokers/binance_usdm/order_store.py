"""The durable record the order service claims before it sends.

`BinanceUsdmOrderService` writes here before a request leaves and finishes the
row after the venue answers, so a process that dies mid-send leaves evidence
that a request may be in flight. Recovery reads it back by client order id.

Two things in here carry the weight.

**The claim is one statement.** `INSERT IGNORE` decides ownership atomically.
A claim decided by a read and then a write has a window between them, and that
window is a duplicate order on a real account.

**`NOT_SENT` is the one state a fresh attempt may follow.** It means the
request provably never left - the transport refused it before any socket was
written - so re-preparing is safe, and it is the only state where that is
true. Every other non-terminal state means a request may have reached Binance,
and the answer to those is recovery, never a second send.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from autotrader.domain.enums import Side
from autotrader.integrations.brokers.binance_usdm.orders import (
    BinanceUsdmFill,
    BinanceUsdmNormalOrderClaim,
    BinanceUsdmNormalOrderRecord,
    BinanceUsdmNormalOrderState,
    BrokerWriteResult,
)
from autotrader.persistence.mysql.models.binance_usdm import BinanceUsdmNormalOrderRow
from autotrader.persistence.mysql.repositories.binance_usdm import (
    BinanceUsdmNormalOrderRepository,
)


class BinanceUsdmOrderRecordMissing(RuntimeError):
    """The row a caller named is not there, and inventing one would lie."""


@dataclass(frozen=True, slots=True)
class MySqlBinanceUsdmNormalOrderStore:
    repository: BinanceUsdmNormalOrderRepository = field(repr=False)

    async def prepare(
        self, record: BinanceUsdmNormalOrderRecord
    ) -> BinanceUsdmNormalOrderClaim:
        if type(record) is not BinanceUsdmNormalOrderRecord:
            raise TypeError("Binance USD-M order record must be exact")
        record.validate()
        if record.state is not BinanceUsdmNormalOrderState.PREPARED:
            raise ValueError("only a prepared Binance USD-M order may be claimed")

        acquired = await self.repository.insert_if_absent(
            {
                "command_id": record.command_id,
                "binding_id": record.binding_id,
                "account_id": record.account_id,
                "client_order_id": record.client_order_id,
                "request_body": record.request_body,
                "request_digest": record.request_digest,
                "prepared_at": record.prepared_at,
                "not_after": record.not_after,
                "dispatch_count": record.dispatch_count,
                "state": record.state.value,
                "result": None,
            }
        )
        if acquired:
            return BinanceUsdmNormalOrderClaim(record=record, acquired=True)

        row = await self.repository.load(record.client_order_id, lock=True)
        if row is None:
            # `INSERT IGNORE` said the row exists and the read says it does
            # not. Nothing true can be returned from here.
            raise BinanceUsdmOrderRecordMissing(
                "Binance USD-M order record vanished between claim and read"
            )
        _require_same_request(row, record)
        if row.state != BinanceUsdmNormalOrderState.NOT_SENT.value:
            return BinanceUsdmNormalOrderClaim(record=_record(row), acquired=False)

        # The previous attempt never reached the socket, so this one may go.
        # `dispatch_count` counts the attempts rather than the sends, which is
        # what makes a record that keeps failing to send visible.
        await self.repository.apply(
            record.client_order_id,
            {
                "state": BinanceUsdmNormalOrderState.PREPARED.value,
                "dispatch_count": row.dispatch_count + 1,
                "prepared_at": record.prepared_at,
                "not_after": record.not_after,
                "result": None,
            },
        )
        retried = await self.repository.load(record.client_order_id)
        if retried is None:
            raise BinanceUsdmOrderRecordMissing(
                "Binance USD-M order record vanished during retry"
            )
        return BinanceUsdmNormalOrderClaim(record=_record(retried), acquired=True)

    async def load_by_client_id(
        self, client_order_id: str
    ) -> BinanceUsdmNormalOrderRecord | None:
        row = await self.repository.load(client_order_id)
        return None if row is None else _record(row)

    async def mark_not_sent(
        self, client_order_id: str, *, request_digest: bytes
    ) -> BinanceUsdmNormalOrderRecord:
        row = await self.repository.load(client_order_id, lock=True)
        if row is None:
            raise BinanceUsdmOrderRecordMissing(
                "Binance USD-M order record to mark is absent"
            )
        if row.request_digest != request_digest:
            # Marking a different request as never sent would licence a send
            # of an order this caller never built.
            raise ValueError("Binance USD-M order digest does not match the record")
        await self.repository.apply(
            client_order_id,
            {"state": BinanceUsdmNormalOrderState.NOT_SENT.value, "result": None},
        )
        return await self._reread(client_order_id)

    async def finish(
        self,
        client_order_id: str,
        *,
        state: BinanceUsdmNormalOrderState,
        result: object | None,
    ) -> BinanceUsdmNormalOrderRecord:
        if type(state) is not BinanceUsdmNormalOrderState:
            raise TypeError("Binance USD-M order state must be exact")
        if state is BinanceUsdmNormalOrderState.ACKNOWLEDGED:
            if type(result) is not BrokerWriteResult:
                raise ValueError("an acknowledged Binance USD-M order needs a result")
            # The column is JSON, so it serialises this itself. Handing it a
            # string would store JSON inside a JSON string and read back as
            # text that no decoder here accepts.
            encoded: dict[str, object] | None = encode_write_result(result)
        elif result is not None:
            raise ValueError("only an acknowledged Binance USD-M order has a result")
        else:
            encoded = None
        touched = await self.repository.apply(
            client_order_id, {"state": state.value, "result": encoded}
        )
        if touched == 0:
            raise BinanceUsdmOrderRecordMissing(
                "Binance USD-M order record to finish is absent"
            )
        return await self._reread(client_order_id)

    async def _reread(self, client_order_id: str) -> BinanceUsdmNormalOrderRecord:
        row = await self.repository.load(client_order_id)
        if row is None:
            raise BinanceUsdmOrderRecordMissing(
                "Binance USD-M order record vanished after a write"
            )
        return _record(row)


def _require_same_request(
    row: BinanceUsdmNormalOrderRow, record: BinanceUsdmNormalOrderRecord
) -> None:
    """One client order id names one request, or recovery cannot use it.

    The id is derived from the command, so a mismatch means two different
    orders were built under one name - and recovery keyed on that name could
    then confirm the wrong one.
    """
    if (
        row.request_digest != record.request_digest
        or row.command_id != record.command_id
        or row.account_id != record.account_id
        or row.binding_id != record.binding_id
    ):
        raise ValueError("Binance USD-M client order id names a different request")


def _record(row: BinanceUsdmNormalOrderRow) -> BinanceUsdmNormalOrderRecord:
    result = row.result
    record = BinanceUsdmNormalOrderRecord(
        command_id=row.command_id,
        account_id=row.account_id,
        binding_id=row.binding_id,
        client_order_id=row.client_order_id,
        request_body=row.request_body,
        request_digest=row.request_digest,
        prepared_at=_utc(row.prepared_at),
        not_after=_utc(row.not_after),
        dispatch_count=row.dispatch_count,
        state=BinanceUsdmNormalOrderState(row.state),
        result=None if result is None else decode_write_result(result),
    )
    record.validate()
    return record


def _utc(value: datetime) -> datetime:
    """MySQL hands back a naive instant; the column stores UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def encode_write_result(result: BrokerWriteResult) -> dict[str, object]:
    """Decimals as text, so what comes back is what went in.

    A float would round the venue's own numbers, and these are the numbers a
    reconciliation later compares against the venue's answer.
    """
    return {
        "broker_order_id": result.broker_order_id,
        "client_order_id": result.client_order_id,
        "provider_state": result.provider_state,
        "cumulative_filled_quantity": str(result.cumulative_filled_quantity),
        "cumulative_quote_quantity": str(result.cumulative_quote_quantity),
        "average_fill_price": str(result.average_fill_price),
        "commissions": [[asset, str(amount)] for asset, amount in result.commissions],
        "fills": [
            {
                "trade_id": fill.trade_id,
                "order_id": fill.order_id,
                "side": fill.side.value,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "commission": str(fill.commission),
                "commission_asset": fill.commission_asset,
                "realized_pnl": str(fill.realized_pnl),
                "occurred_at": fill.occurred_at.isoformat(),
            }
            for fill in result.fills
        ],
        "recovered": result.recovered,
    }


def decode_write_result(payload: object) -> BrokerWriteResult:
    if type(payload) is not dict:
        raise ValueError("Binance USD-M persisted order result is invalid")
    body = cast(dict[str, object], payload)
    fills = body["fills"]
    if type(fills) is not list:
        raise ValueError("Binance USD-M persisted order fills are invalid")
    commissions = body["commissions"]
    if type(commissions) is not list:
        raise ValueError("Binance USD-M persisted order commissions are invalid")
    result = BrokerWriteResult(
        broker_order_id=_text(body["broker_order_id"]),
        client_order_id=_text(body["client_order_id"]),
        provider_state=_text(body["provider_state"]),
        cumulative_filled_quantity=_decimal(body["cumulative_filled_quantity"]),
        cumulative_quote_quantity=_decimal(body["cumulative_quote_quantity"]),
        average_fill_price=_decimal(body["average_fill_price"]),
        commissions=tuple(
            (_text(pair[0]), _decimal(pair[1]))
            for pair in cast(list[list[object]], commissions)
        ),
        fills=tuple(
            _fill(cast(dict[str, object], entry)) for entry in cast(list[object], fills)
        ),
        recovered=_boolean(body["recovered"]),
    )
    result.__post_init__()
    return result


def _fill(entry: dict[str, object]) -> BinanceUsdmFill:
    return BinanceUsdmFill(
        trade_id=_integer(entry["trade_id"]),
        order_id=_integer(entry["order_id"]),
        side=Side(_text(entry["side"])),
        quantity=_decimal(entry["quantity"]),
        price=_decimal(entry["price"]),
        commission=_decimal(entry["commission"]),
        commission_asset=_text(entry["commission_asset"]),
        realized_pnl=_decimal(entry["realized_pnl"]),
        occurred_at=datetime.fromisoformat(_text(entry["occurred_at"])),
    )


def _text(value: object) -> str:
    if type(value) is not str:
        raise ValueError("Binance USD-M persisted order text is invalid")
    return value


def _decimal(value: object) -> Decimal:
    if type(value) is not str:
        raise ValueError("Binance USD-M persisted order decimal is invalid")
    return Decimal(value)


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("Binance USD-M persisted order integer is invalid")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("Binance USD-M persisted order boolean is invalid")
    return value


__all__ = (
    "BinanceUsdmOrderRecordMissing",
    "MySqlBinanceUsdmNormalOrderStore",
    "decode_write_result",
    "encode_write_result",
)
