"""The protective stop's durable record.

`BinanceUsdmProtectionService` claims a row before it places a stop, so a
position that filled is never left with nothing written down about whether it
is protected. The row carries `protection_deadline`: the instant by which a
stop must be confirmed or the position closed instead, which is what keeps an
unprotected position finite rather than merely unlikely.

There is no `NOT_SENT` here, and that asymmetry with the normal order store is
deliberate rather than an omission. An entry that was never sent is simply an
entry that did not happen. A protective stop that was never sent sits behind a
position that already exists, so the answer is never "try again as if nothing
occurred" - it is recovery, and failing that, the emergency close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from autotrader.domain.enums import Side
from autotrader.integrations.brokers.binance_usdm.algo_orders import (
    BinanceUsdmAlgoOrderClaim,
    BinanceUsdmAlgoOrderRecord,
    BinanceUsdmAlgoOrderState,
    EntryFill,
    ProtectionResult,
)
from autotrader.integrations.brokers.binance_usdm.order_store import (
    BinanceUsdmOrderRecordMissing,
    decode_write_result,
    encode_write_result,
)
from autotrader.persistence.mysql.models.binance_usdm import BinanceUsdmAlgoOrderRow
from autotrader.persistence.mysql.repositories.binance_usdm import (
    BinanceUsdmAlgoOrderRepository,
)

_SAFE_STATES = frozenset(
    {
        BinanceUsdmAlgoOrderState.ACTIVE,
        BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED,
    }
)


@dataclass(frozen=True, slots=True)
class MySqlBinanceUsdmAlgoOrderStore:
    repository: BinanceUsdmAlgoOrderRepository = field(repr=False)

    async def prepare(
        self, record: BinanceUsdmAlgoOrderRecord
    ) -> BinanceUsdmAlgoOrderClaim:
        if type(record) is not BinanceUsdmAlgoOrderRecord:
            raise TypeError("Binance USD-M algo record must be exact")
        record.validate()
        if record.state is not BinanceUsdmAlgoOrderState.PREPARED:
            raise ValueError("only prepared Binance USD-M protection may be claimed")

        fill = record.entry_fill
        acquired = await self.repository.insert_if_absent(
            {
                "placement_command_id": record.placement_id,
                "entry_command_id": fill.entry_command_id,
                "client_algo_id": record.client_algo_id,
                "binding_id": fill.binding_id,
                "account_id": fill.account_id,
                "instrument_id": fill.instrument_id,
                "emergency_close_command_id": fill.emergency_close_command_id,
                "side": fill.side.value,
                "symbol": fill.symbol,
                "first_fill_quantity": fill.first_fill_quantity,
                "cumulative_quantity_before": fill.cumulative_quantity_before,
                "average_fill_price": fill.average_fill_price,
                "tick_size": fill.tick_size,
                "trigger_price": record.trigger_price,
                "filled_at": fill.filled_at,
                "protection_deadline": fill.protection_deadline,
                "prepared_at": record.prepared_at,
                "request_body": record.request_body,
                "request_digest": record.request_digest,
                "state": record.state.value,
                "result": None,
            }
        )
        if acquired:
            return BinanceUsdmAlgoOrderClaim(record=record, acquired=True)

        row = await self.repository.load(record.client_algo_id, lock=True)
        if row is None:
            raise BinanceUsdmOrderRecordMissing(
                "Binance USD-M protection record vanished between claim and read"
            )
        _require_same_request(row, record)
        # Whatever state it is in, it is not this caller's to place. A second
        # stop behind one position is a state nothing downstream would flag,
        # because two working stops both look like protection.
        return BinanceUsdmAlgoOrderClaim(record=_record(row), acquired=False)

    async def load_by_client_algo_id(
        self, client_algo_id: str
    ) -> BinanceUsdmAlgoOrderRecord | None:
        row = await self.repository.load(client_algo_id)
        return None if row is None else _record(row)

    async def finish(
        self,
        client_algo_id: str,
        *,
        state: BinanceUsdmAlgoOrderState,
        result: ProtectionResult | None,
    ) -> BinanceUsdmAlgoOrderRecord:
        if type(state) is not BinanceUsdmAlgoOrderState:
            raise TypeError("Binance USD-M algo state must be exact")
        if state in _SAFE_STATES:
            if type(result) is not ProtectionResult:
                raise ValueError("safe Binance USD-M protection needs a result")
            if result.state is not state:
                raise ValueError("Binance USD-M protection result state differs")
            if result.client_algo_id != client_algo_id:
                raise ValueError("Binance USD-M protection result identity differs")
            encoded: dict[str, object] | None = _encode(result)
        elif result is not None:
            raise ValueError("unsafe Binance USD-M protection cannot have a result")
        else:
            encoded = None
        touched = await self.repository.apply(
            client_algo_id, {"state": state.value, "result": encoded}
        )
        if touched == 0:
            raise BinanceUsdmOrderRecordMissing(
                "Binance USD-M protection record to finish is absent"
            )
        row = await self.repository.load(client_algo_id)
        if row is None:
            raise BinanceUsdmOrderRecordMissing(
                "Binance USD-M protection record vanished after a write"
            )
        return _record(row)


def _require_same_request(
    row: BinanceUsdmAlgoOrderRow, record: BinanceUsdmAlgoOrderRecord
) -> None:
    """One client algo id names one stop behind one fill.

    The id derives from the entry command, so a mismatch means two different
    stops were built under one name and recovery could confirm the wrong one.
    """
    if (
        row.request_digest != record.request_digest
        or row.entry_command_id != record.entry_fill.entry_command_id
        or row.account_id != record.entry_fill.account_id
        or row.binding_id != record.entry_fill.binding_id
    ):
        raise ValueError("Binance USD-M client algo id names a different stop")


def _record(row: BinanceUsdmAlgoOrderRow) -> BinanceUsdmAlgoOrderRecord:
    result = row.result
    record = BinanceUsdmAlgoOrderRecord(
        entry_fill=EntryFill(
            entry_command_id=row.entry_command_id,
            account_id=row.account_id,
            instrument_id=row.instrument_id,
            binding_id=row.binding_id,
            side=Side(row.side),
            first_fill_quantity=row.first_fill_quantity,
            cumulative_quantity_before=row.cumulative_quantity_before,
            average_fill_price=row.average_fill_price,
            symbol=row.symbol,
            tick_size=row.tick_size,
            filled_at=_utc(row.filled_at),
            protection_deadline=_utc(row.protection_deadline),
            emergency_close_command_id=row.emergency_close_command_id,
        ),
        client_algo_id=row.client_algo_id,
        # The first stop's placement id is the entry's, and the record says
        # that by leaving it absent. Reconstructing it any other way would
        # make a reloaded record differ from the one that was written.
        placement_command_id=(
            None
            if row.placement_command_id == row.entry_command_id
            else row.placement_command_id
        ),
        trigger_price=row.trigger_price,
        request_body=row.request_body,
        request_digest=row.request_digest,
        prepared_at=_utc(row.prepared_at),
        state=BinanceUsdmAlgoOrderState(row.state),
        result=None if result is None else _decode(result),
    )
    record.validate()
    return record


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _encode(result: ProtectionResult) -> dict[str, object]:
    close = result.emergency_close
    return {
        "provider_algo_id": result.provider_algo_id,
        "client_algo_id": result.client_algo_id,
        "state": result.state.value,
        "trigger_price": str(result.trigger_price),
        "recovered": result.recovered,
        # The emergency close is the evidence that a position was
        # flattened because it could not be protected, so it is kept whole
        # rather than reduced to its identifier.
        "emergency_close": None if close is None else encode_write_result(close),
    }


def _decode(payload: object) -> ProtectionResult:
    if type(payload) is not dict:
        raise ValueError("Binance USD-M persisted protection result is invalid")
    body = cast(dict[str, object], payload)
    close = body["emergency_close"]
    provider = body["provider_algo_id"]
    if provider is not None and type(provider) is not str:
        raise ValueError("Binance USD-M persisted provider algo id is invalid")
    result = ProtectionResult(
        provider_algo_id=provider,
        client_algo_id=_text(body["client_algo_id"]),
        state=BinanceUsdmAlgoOrderState(_text(body["state"])),
        trigger_price=Decimal(_text(body["trigger_price"])),
        recovered=_boolean(body["recovered"]),
        emergency_close=None if close is None else decode_write_result(close),
    )
    result.__post_init__()
    return result


def _text(value: object) -> str:
    if type(value) is not str:
        raise ValueError("Binance USD-M persisted protection text is invalid")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("Binance USD-M persisted protection boolean is invalid")
    return value


__all__ = ("MySqlBinanceUsdmAlgoOrderStore",)
