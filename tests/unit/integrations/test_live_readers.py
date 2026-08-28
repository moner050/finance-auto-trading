"""Each broker's answer, translated.

The translators are small, and the thing worth checking about each is the same:
that a working order keeps the identity the writer recorded, and that a
finished one is not reported as working. An order id built a second way here
would read as an order somebody else placed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.integrations.brokers.binance_usdm.account import (
    BinanceUsdmAccountSnapshot,
    BinanceUsdmBalance,
    BinanceUsdmOpenOrder,
    BinanceUsdmPosition,
)
from autotrader.integrations.brokers.binance_usdm.orders import (
    binance_provider_order_id,
)
from autotrader.integrations.brokers.kis.account_snapshot_contracts import (
    KisDomesticCashEnvironment,
    KisKrDomesticCashPosition,
    KisStableKrDomesticCashAccountSnapshot,
)
from autotrader.integrations.brokers.kis.cash_order_recovery import KisDailyOrder
from autotrader.integrations.brokers.kis.cash_writer import kis_provider_order_id
from autotrader.integrations.brokers.live_readers import (
    SnapshotIdentity,
    binance_reader,
    binance_reported,
    kis_reported,
    toss_reported,
)
from autotrader.integrations.brokers.live_snapshots import ReportedAccount
from autotrader.integrations.brokers.toss.us_account_snapshot import (
    TossUsAccountSnapshot,
    TossUsCashFact,
    TossUsPositionFact,
)
from autotrader.integrations.brokers.toss.us_cash_writer import toss_provider_order_id
from autotrader.integrations.brokers.toss.us_orders import TossUsOrderFact
from autotrader.shared.ids import new_uuid7

NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)


class _Registry:
    def __init__(self, known: dict[str, UUID]) -> None:
        self._known = known

    async def resolve(self, exchange_code: str, code: str) -> UUID:
        del exchange_code
        if code not in self._known:
            raise LookupError(code)
        return self._known[code]


# --- KIS ------------------------------------------------------------------


def _kis_order(**changes: object) -> KisDailyOrder:
    values: dict[str, object] = {
        "binding_id": new_uuid7(),
        "order_date": "20260827",
        "organization_number": "00001",
        "order_number": "0000000001",
        "original_order_number": "0000000000",
        "provider_timestamp": NOW,
        "side": Side.BUY,
        "symbol": "005930",
        "order_style": OrderStyle.LIMIT,
        "order_quantity": Decimal("10"),
        "limit_price": Decimal("70000"),
        "cumulative_filled_quantity": Decimal("0"),
        "average_fill_price": Decimal("0"),
        "total_filled_amount": Decimal("0"),
        "confirmed_cancelled_quantity": Decimal("0"),
        "remaining_quantity": Decimal("10"),
        "rejected_quantity": Decimal("0"),
        "fee_amount": None,
    }
    values.update(changes)
    return KisDailyOrder(**values)  # type: ignore[arg-type]


def _kis_snapshot() -> KisStableKrDomesticCashAccountSnapshot:
    return KisStableKrDomesticCashAccountSnapshot.build(
        observed_at=NOW,
        environment=KisDomesticCashEnvironment.REAL,
        total_deposit_cash=Decimal("1000000"),
        positions=(
            KisKrDomesticCashPosition(
                symbol="005930",
                total_quantity=Decimal("10"),
                order_available_quantity=Decimal("10"),
            ),
        ),
    )


def test_a_kis_order_keeps_the_identity_the_writer_recorded() -> None:
    reported = kis_reported(_kis_snapshot(), (_kis_order(),))

    assert reported.open_orders[0].broker_order_id == kis_provider_order_id(
        "20260827", "00001", "0000000001"
    )


def test_a_fully_filled_kis_order_is_not_working() -> None:
    reported = kis_reported(
        _kis_snapshot(),
        (
            _kis_order(
                remaining_quantity=Decimal("0"),
                cumulative_filled_quantity=Decimal("10"),
                average_fill_price=Decimal("70000"),
                total_filled_amount=Decimal("700000"),
            ),
        ),
    )

    assert reported.open_orders == ()


def test_kis_reports_no_client_order_id() -> None:
    """It has no such field, and inventing one would compare unequal to ours."""
    reported = kis_reported(_kis_snapshot(), (_kis_order(),))

    assert reported.open_orders[0].broker_client_order_id is None


def test_kis_positions_carry_the_total_quantity() -> None:
    reported = kis_reported(_kis_snapshot(), ())

    assert reported.positions[0].symbol == "005930"
    assert reported.positions[0].quantity == Decimal("10")


# --- Toss -----------------------------------------------------------------


def _toss_order(**changes: object) -> TossUsOrderFact:
    values: dict[str, object] = {
        "provider_order_id": "abc-123",
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": Decimal("5"),
        "cumulative_fill_quantity": Decimal("0"),
        "state": "PLACED",
        "limit_price": Decimal("190"),
        "commission": None,
        "tax": None,
        "settlement_asset": "USD",
        "ordered_at": NOW,
        "filled_at": None,
        "canceled_at": None,
        "provider_as_of": NOW,
        "captured_at": NOW,
        "source_digest": b"d" * 32,
    }
    values.update(changes)
    return TossUsOrderFact(**values)  # type: ignore[arg-type]


def _toss_snapshot() -> TossUsAccountSnapshot:
    return TossUsAccountSnapshot(
        account_scope_digest=b"s" * 32,
        cash_fact=TossUsCashFact(
            state="SETTLED",
            available_cash=Decimal("2000"),
            settled_cash=Decimal("2000"),
            source_field="availableCash",
            provider_as_of=NOW,
            captured_at=NOW,
            source_digest=b"c" * 32,
        ),
        positions=(
            TossUsPositionFact(
                symbol="AAPL",
                total_quantity=Decimal("5"),
                sellable_quantity=Decimal("5"),
                average_price=Decimal("180"),
                market_value=Decimal("950"),
                provider_as_of=NOW,
                captured_at=NOW,
                source_digest=b"p" * 32,
            ),
        ),
        holdings_page_count=1,
        sellable_page_count=1,
        source_digest=b"t" * 32,
    )


def test_a_toss_order_keeps_the_identity_the_writer_recorded() -> None:
    reported = toss_reported(_toss_snapshot(), (_toss_order(),))

    assert reported.open_orders[0].broker_order_id == toss_provider_order_id("abc-123")


def test_a_finished_toss_order_is_not_working() -> None:
    reported = toss_reported(_toss_snapshot(), (_toss_order(state="FILLED"),))

    assert reported.open_orders == ()


def test_toss_reports_no_client_order_id() -> None:
    """Its order list omits the client id it accepted on submission."""
    reported = toss_reported(_toss_snapshot(), (_toss_order(),))

    assert reported.open_orders[0].broker_client_order_id is None


# --- Binance --------------------------------------------------------------


def _binance_position(**changes: object) -> BinanceUsdmPosition:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "position_side": "BOTH",
        "amount": Decimal("0.5"),
        "entry_price": Decimal("60000"),
        "mark_price": Decimal("60500"),
        "unrealized_pnl": Decimal("250"),
        "isolated_margin": Decimal("0"),
        "notional": Decimal("30250"),
        "margin_asset": "USDT",
        "initial_margin": Decimal("100"),
        "maintenance_margin": Decimal("50"),
        "position_initial_margin": Decimal("100"),
        "open_order_initial_margin": Decimal("0"),
        "updated_at": NOW,
    }
    values.update(changes)
    return BinanceUsdmPosition(**values)  # type: ignore[arg-type]


def _binance_order(**changes: object) -> BinanceUsdmOpenOrder:
    values: dict[str, object] = {
        "order_id": 4242,
        "client_order_id": "ours-1",
        "symbol": "BTCUSDT",
        "status": "NEW",
        "side": "BUY",
        "order_type": "LIMIT",
        "executed_quantity": Decimal("0"),
        "original_quantity": Decimal("0.5"),
        "reduce_only": False,
        "close_position": False,
    }
    values.update(changes)
    return BinanceUsdmOpenOrder(**values)  # type: ignore[arg-type]


def _binance_snapshot(**changes: object) -> BinanceUsdmAccountSnapshot:
    values: dict[str, object] = {
        "as_of": NOW,
        "balances": (
            BinanceUsdmBalance(
                asset="USDT",
                balance=Decimal("2000"),
                available_balance=Decimal("1900"),
                maximum_withdraw_amount=Decimal("1900"),
                updated_at=NOW,
            ),
        ),
        "positions": (_binance_position(),),
        "normal_orders": (_binance_order(),),
        "algo_orders": (),
        "trades": (),
        "income": (),
    }
    values.update(changes)
    return BinanceUsdmAccountSnapshot(**values)  # type: ignore[arg-type]


def test_a_binance_order_keeps_the_identity_the_writer_recorded() -> None:
    reported = binance_reported(_binance_snapshot())

    assert reported.open_orders[0].broker_order_id == binance_provider_order_id(4242)


def test_binance_carries_our_client_order_id_back() -> None:
    reported = binance_reported(_binance_snapshot())

    assert reported.open_orders[0].broker_client_order_id == "ours-1"


def test_a_filled_binance_order_is_not_working() -> None:
    reported = binance_reported(
        _binance_snapshot(normal_orders=(_binance_order(status="FILLED"),))
    )

    assert reported.open_orders == ()


def test_a_binance_symbol_at_zero_is_dropped_when_assembled() -> None:
    """It lists every symbol it has ever margined, most of them flat."""
    snapshot = _binance_snapshot(
        positions=(
            _binance_position(),
            _binance_position(symbol="ETHUSDT", amount=Decimal("0")),
        )
    )
    reported = binance_reported(snapshot)
    reader = binance_reader(
        identity=SnapshotIdentity(broker_id=new_uuid7(), account_id=ACCOUNT),
        capture=_capture(reported),
        resolver=_Registry({"BTCUSDT": INSTRUMENT}),
    )

    result = asyncio.run(reader.read_snapshot(account_id=ACCOUNT, now=NOW))

    # ETHUSDT is not resolvable, and must not need to be: it is not held.
    assert len(result.positions) == 1
    assert result.positions[0].instrument_id == INSTRUMENT


ACCOUNT = new_uuid7()
INSTRUMENT = new_uuid7()


def _capture(reported: ReportedAccount) -> object:
    async def capture(now: datetime) -> ReportedAccount:
        del now
        return reported

    return capture


def test_a_reader_answers_for_one_account_only() -> None:
    reader = binance_reader(
        identity=SnapshotIdentity(broker_id=new_uuid7(), account_id=ACCOUNT),
        capture=_capture(binance_reported(_binance_snapshot())),  # type: ignore[arg-type]
        resolver=_Registry({"BTCUSDT": INSTRUMENT}),
    )

    with pytest.raises(ValueError, match="one account only"):
        asyncio.run(reader.read_snapshot(account_id=new_uuid7(), now=NOW))
