"""Store logic that does not need a database to be wrong.

The claim semantics and the codecs are the two places a mistake here would be
silent, and both are decidable without MySQL: a fake repository can answer
`insert_if_absent` the way `INSERT IGNORE` does, and the codecs are pure.

What is deliberately *not* covered here is the schema - the CHECK constraints,
the foreign keys, the uniqueness. A fake satisfies those by construction, so
claiming them here would be claiming something untested. They are in
tests/integration/persistence/test_binance_usdm_order_stores.py, which needs a
disposable MySQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid7

import pytest

from autotrader.domain.enums import Side
from autotrader.integrations.brokers.binance_usdm.algo_order_store import (
    MySqlBinanceUsdmAlgoOrderStore,
)
from autotrader.integrations.brokers.binance_usdm.algo_orders import (
    BinanceUsdmAlgoOrderRecord,
    BinanceUsdmAlgoOrderState,
    EntryFill,
    ProtectionResult,
    binance_protection_client_algo_id,
)
from autotrader.integrations.brokers.binance_usdm.order_store import (
    BinanceUsdmOrderRecordMissing,
    MySqlBinanceUsdmNormalOrderStore,
    decode_write_result,
    encode_write_result,
)
from autotrader.integrations.brokers.binance_usdm.orders import (
    BinanceUsdmFill,
    BinanceUsdmNormalOrderRecord,
    BinanceUsdmNormalOrderState,
    BrokerWriteResult,
)

NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
BODY = b"symbol=BTCUSDT&side=BUY&type=MARKET&quantity=0.002"


@dataclass
class _Row:
    """Whatever the store reads back, held by attribute like the ORM row."""

    values: dict[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError:
            raise AttributeError(name) from None


@dataclass
class FakeRepository:
    """`INSERT IGNORE` and a keyed update, with nothing else pretended."""

    key: str
    rows: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    locks: list[str] = field(default_factory=list[str])

    async def insert_if_absent(self, values: dict[str, Any]) -> bool:
        identity = values[self.key]
        if identity in self.rows:
            return False
        self.rows[identity] = dict(values)
        return True

    async def load(self, identity: str, *, lock: bool = False) -> _Row | None:
        if lock:
            self.locks.append(identity)
        row = self.rows.get(identity)
        return None if row is None else _Row(dict(row))

    async def apply(self, identity: str, values: dict[str, Any]) -> int:
        if identity not in self.rows:
            return 0
        self.rows[identity].update(values)
        return 1


def _write_result(client_order_id: str | None = None) -> BrokerWriteResult:
    return BrokerWriteResult(
        broker_order_id="BINANCE-USDM:8389765812345678901",
        client_order_id=client_order_id or f"v6-{uuid7().hex}",
        provider_state="FILLED",
        cumulative_filled_quantity=Decimal("0.002"),
        cumulative_quote_quantity=Decimal("221.4130000000000001"),
        average_fill_price=Decimal("110706.50000000000005"),
        commissions=(("USDT", Decimal("0.08856520000000000004")),),
        fills=(
            BinanceUsdmFill(
                trade_id=4321,
                order_id=8389765812345678901,
                side=Side.BUY,
                quantity=Decimal("0.002"),
                price=Decimal("110706.50000000000005"),
                commission=Decimal("0.08856520000000000004"),
                commission_asset="USDT",
                realized_pnl=Decimal("-0.0000000000000001"),
                occurred_at=NOW,
            ),
        ),
        recovered=False,
    )


def test_write_result_survives_the_codec_exactly() -> None:
    """A float round trip loses these, which is why they are strings."""
    original = _write_result()
    assert decode_write_result(encode_write_result(original)) == original


def _normal(body: bytes = BODY) -> BinanceUsdmNormalOrderRecord:
    command_id = uuid7()
    record = BinanceUsdmNormalOrderRecord(
        command_id=command_id,
        account_id=uuid7(),
        binding_id=uuid7(),
        client_order_id=f"v6-{command_id.hex}",
        request_body=body,
        request_digest=sha256(body).digest(),
        prepared_at=NOW,
        not_after=NOW + timedelta(minutes=1),
        dispatch_count=1,
        state=BinanceUsdmNormalOrderState.PREPARED,
        result=None,
    )
    record.validate()
    return record


def _normal_store() -> tuple[MySqlBinanceUsdmNormalOrderStore, FakeRepository]:
    repository = FakeRepository(key="client_order_id")
    return MySqlBinanceUsdmNormalOrderStore(repository), repository  # pyright: ignore[reportArgumentType]


@pytest.mark.asyncio
async def test_one_caller_owns_the_claim() -> None:
    store, _ = _normal_store()
    record = _normal()
    assert (await store.prepare(record)).acquired is True
    second = await store.prepare(record)
    assert second.acquired is False
    assert second.record.dispatch_count == 1


@pytest.mark.asyncio
async def test_not_sent_is_the_only_state_a_fresh_attempt_may_follow() -> None:
    store, _ = _normal_store()
    record = _normal()
    await store.prepare(record)
    await store.mark_not_sent(
        record.client_order_id, request_digest=record.request_digest
    )

    retry = await store.prepare(record)
    assert retry.acquired is True
    assert retry.record.dispatch_count == 2

    for state in (
        BinanceUsdmNormalOrderState.AMBIGUOUS,
        BinanceUsdmNormalOrderState.UNKNOWN,
        BinanceUsdmNormalOrderState.REJECTED,
    ):
        await store.finish(record.client_order_id, state=state, result=None)
        blocked = await store.prepare(record)
        assert blocked.acquired is False, state
        assert blocked.record.state is state


@pytest.mark.asyncio
async def test_the_claim_is_read_under_a_lock() -> None:
    """The losing caller reads the row it did not write, and must not race
    a concurrent transition while deciding what to do with it."""
    store, repository = _normal_store()
    record = _normal()
    await store.prepare(record)
    await store.prepare(record)
    assert repository.locks == [record.client_order_id]


@pytest.mark.asyncio
async def test_one_client_order_id_names_one_request() -> None:
    store, _ = _normal_store()
    record = _normal()
    await store.prepare(record)

    other = b"symbol=BTCUSDT&side=SELL&type=MARKET&quantity=0.500"
    impostor = BinanceUsdmNormalOrderRecord(
        command_id=record.command_id,
        account_id=record.account_id,
        binding_id=record.binding_id,
        client_order_id=record.client_order_id,
        request_body=other,
        request_digest=sha256(other).digest(),
        prepared_at=NOW,
        not_after=NOW + timedelta(minutes=1),
        dispatch_count=1,
        state=BinanceUsdmNormalOrderState.PREPARED,
        result=None,
    )
    with pytest.raises(ValueError):
        await store.prepare(impostor)
    with pytest.raises(ValueError):
        await store.mark_not_sent(
            record.client_order_id, request_digest=sha256(other).digest()
        )


@pytest.mark.asyncio
async def test_a_result_exists_exactly_when_the_venue_answered() -> None:
    store, _ = _normal_store()
    record = _normal()
    await store.prepare(record)

    with pytest.raises(ValueError):
        await store.finish(
            record.client_order_id,
            state=BinanceUsdmNormalOrderState.ACKNOWLEDGED,
            result=None,
        )
    with pytest.raises(ValueError):
        await store.finish(
            record.client_order_id,
            state=BinanceUsdmNormalOrderState.REJECTED,
            result=_write_result(record.client_order_id),
        )
    with pytest.raises(BinanceUsdmOrderRecordMissing):
        await store.finish(
            f"v6-{uuid7().hex}",
            state=BinanceUsdmNormalOrderState.REJECTED,
            result=None,
        )


@pytest.mark.asyncio
async def test_a_claim_that_vanishes_is_not_invented() -> None:
    """`INSERT IGNORE` said the row exists and the read says it does not.
    Nothing true can be returned, so nothing is."""
    store, repository = _normal_store()
    record = _normal()
    await store.prepare(record)
    repository.rows.clear()
    repository.rows[record.client_order_id] = {}
    del repository.rows[record.client_order_id]
    repository.rows["someone-else"] = {"client_order_id": "someone-else"}

    class _Vanishing(FakeRepository):
        async def insert_if_absent(self, values: dict[str, Any]) -> bool:
            return False

        async def load(self, identity: str, *, lock: bool = False) -> _Row | None:
            return None

    vanishing = MySqlBinanceUsdmNormalOrderStore(
        _Vanishing(key="client_order_id")  # pyright: ignore[reportArgumentType]
    )
    with pytest.raises(BinanceUsdmOrderRecordMissing):
        await vanishing.prepare(record)


def _fill(entry_command_id: UUID | None = None) -> EntryFill:
    return EntryFill(
        entry_command_id=entry_command_id or uuid7(),
        account_id=uuid7(),
        instrument_id=uuid7(),
        binding_id=uuid7(),
        side=Side.BUY,
        first_fill_quantity=Decimal("0.002"),
        cumulative_quantity_before=Decimal(0),
        average_fill_price=Decimal("110706.50000000000005"),
        symbol="BTCUSDT",
        tick_size=Decimal("0.10"),
        filled_at=NOW,
        protection_deadline=NOW + timedelta(seconds=30),
        emergency_close_command_id=uuid7(),
    )


def _algo(fill: EntryFill) -> BinanceUsdmAlgoOrderRecord:
    body = b"algoType=CONDITIONAL&symbol=BTCUSDT&type=STOP_MARKET"
    record = BinanceUsdmAlgoOrderRecord(
        entry_fill=fill,
        client_algo_id=binance_protection_client_algo_id(fill.entry_command_id),
        trigger_price=Decimal("109500.30000000000004"),
        request_body=body,
        request_digest=sha256(body).digest(),
        prepared_at=NOW,
        state=BinanceUsdmAlgoOrderState.PREPARED,
        result=None,
    )
    record.validate()
    return record


def _algo_store() -> MySqlBinanceUsdmAlgoOrderStore:
    return MySqlBinanceUsdmAlgoOrderStore(
        FakeRepository(key="client_algo_id")  # pyright: ignore[reportArgumentType]
    )


@pytest.mark.asyncio
async def test_a_second_stop_is_never_placed_behind_one_position() -> None:
    """Two working stops both look like protection, so nothing downstream
    would flag the state this refusal prevents."""
    store = _algo_store()
    record = _algo(_fill())
    assert (await store.prepare(record)).acquired is True

    for state in (
        BinanceUsdmAlgoOrderState.PREPARED,
        BinanceUsdmAlgoOrderState.AMBIGUOUS,
        BinanceUsdmAlgoOrderState.UNKNOWN,
        BinanceUsdmAlgoOrderState.REJECTED,
    ):
        if state is not BinanceUsdmAlgoOrderState.PREPARED:
            await store.finish(record.client_algo_id, state=state, result=None)
        again = await store.prepare(record)
        assert again.acquired is False, state


@pytest.mark.asyncio
async def test_protection_round_trips_with_its_emergency_close() -> None:
    store = _algo_store()
    fill = _fill()
    record = _algo(fill)
    await store.prepare(record)

    closed = ProtectionResult(
        provider_algo_id=None,
        client_algo_id=record.client_algo_id,
        state=BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED,
        trigger_price=record.trigger_price,
        recovered=True,
        emergency_close=_write_result(),
    )
    await store.finish(
        record.client_algo_id,
        state=BinanceUsdmAlgoOrderState.EMERGENCY_CLOSED,
        result=closed,
    )
    reloaded = await store.load_by_client_algo_id(record.client_algo_id)
    assert reloaded is not None
    assert reloaded.result == closed
    assert reloaded.entry_fill == fill


@pytest.mark.asyncio
async def test_protection_result_must_name_its_own_stop() -> None:
    store = _algo_store()
    record = _algo(_fill())
    await store.prepare(record)

    stranger = _algo(_fill())
    mismatched = ProtectionResult(
        provider_algo_id="BINANCE-USDM-ALGO:1234567890123456789",
        client_algo_id=stranger.client_algo_id,
        state=BinanceUsdmAlgoOrderState.ACTIVE,
        trigger_price=record.trigger_price,
        recovered=False,
        emergency_close=None,
    )
    with pytest.raises(ValueError):
        await store.finish(
            record.client_algo_id,
            state=BinanceUsdmAlgoOrderState.ACTIVE,
            result=mismatched,
        )
