"""Translating one Binance trade into one ledger execution.

Only the translation. What it must not do is invent anything the venue did
not say, and the refusals below are where that line is drawn.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from autotrader.domain.enums import Side
from autotrader.execution.fills.models import ChargeBasis, ChargeEffect, ChargeLegRole
from autotrader.integrations.brokers.binance_usdm.account import BinanceUsdmTradeFact
from autotrader.integrations.brokers.binance_usdm.fills import (
    SOURCE_PARTITION,
    BinanceUsdmFillUnsupported,
    binance_execution_event,
    binance_trade_execution_id,
    trade_hash,
)
from autotrader.shared.ids import new_uuid7

NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


def trade(**changes: object) -> BinanceUsdmTradeFact:
    values: dict[str, object] = {
        "trade_id": 918273645,
        "order_id": 8389765812345678901,
        "symbol": "BTCUSDT",
        "side": "BUY",
        "quantity": Decimal("0.002"),
        "price": Decimal("110706.50000000000005"),
        "commission": Decimal("0.08856520000000000004"),
        "commission_asset": "USDT",
        "realized_pnl": Decimal("-0.0000000000000001"),
        "occurred_at": NOW,
    }
    values.update(changes)
    return BinanceUsdmTradeFact(**values)  # pyright: ignore[reportArgumentType]


def event(fact: BinanceUsdmTradeFact, leg: ChargeLegRole = ChargeLegRole.ENTRY):
    return binance_execution_event(
        trade=fact,
        account_id=new_uuid7(),
        instrument_id=new_uuid7(),
        broker_id=new_uuid7(),
        order_id=new_uuid7(),
        broker_order_id="BINANCE-USDM:8389765812345678901",
        broker_client_order_id=f"v6-{new_uuid7().hex}",
        currency="USD",
        leg_role=leg,
        observed_at=NOW,
    )


def test_the_venues_trade_id_is_what_the_ledger_deduplicates_on() -> None:
    """A settlement pass that overlaps the previous one must add nothing, and
    a time window that must not overlap is not a guarantee."""
    built = event(trade())
    assert built.broker_execution_id == "BINANCE-USDM-TRADE:918273645"
    assert built.source_sequence == 918273645
    assert built.source_partition == SOURCE_PARTITION
    assert binance_trade_execution_id(918273645) == built.broker_execution_id


def test_price_and_quantity_come_across_undamaged() -> None:
    built = event(trade())
    assert built.price == Decimal("110706.50000000000005")
    assert built.quantity == Decimal("0.002")
    assert built.executed_at == NOW
    assert built.side is Side.BUY


def test_an_exit_fill_carries_the_side_it_actually_traded() -> None:
    built = event(trade(side="SELL"), ChargeLegRole.EXIT_STOP)
    assert built.side is Side.SELL
    assert built.charges[0].leg_role is ChargeLegRole.EXIT_STOP


def test_the_commission_becomes_one_charge() -> None:
    built = event(trade())
    assert len(built.charges) == 1
    charge = built.charges[0]
    assert charge.amount == Decimal("0.08856520000000000004")
    assert charge.currency == "USD"
    assert charge.effect is ChargeEffect.DEBIT
    assert charge.charge_basis is ChargeBasis.PER_UNIT
    assert charge.basis_quantity == Decimal("0.002")


def test_a_rebated_fill_has_no_charge_rather_than_a_zero_one() -> None:
    built = event(trade(commission=Decimal(0)))
    assert built.charges == ()


def test_a_commission_in_another_asset_is_refused() -> None:
    """Recording BNB under the settlement currency puts a wrong number in the
    ledger; dropping it loses real money from the ledger."""
    with pytest.raises(BinanceUsdmFillUnsupported):
        event(trade(commission_asset="BNB"))


def test_another_symbol_is_refused() -> None:
    with pytest.raises(BinanceUsdmFillUnsupported):
        event(trade(symbol="ETHUSDT"))


def test_an_unreadable_side_is_refused() -> None:
    with pytest.raises(BinanceUsdmFillUnsupported):
        event(trade(side="LONG"))


def test_the_hash_changes_when_the_venue_says_something_different() -> None:
    """So a replay that disagrees with what was recorded is visible rather
    than silently overwriting it."""
    base = trade()
    assert trade_hash(base) == trade_hash(trade())
    for change in (
        {"price": Decimal("110706.5")},
        {"quantity": Decimal("0.003")},
        {"commission": Decimal("0.09")},
        {"realized_pnl": Decimal("0")},
        {"order_id": 8389765812345678902},
    ):
        assert trade_hash(base) != trade_hash(trade(**change)), change


def test_a_zero_price_or_size_is_refused() -> None:
    for change in ({"quantity": Decimal(0)}, {"price": Decimal(0)}):
        with pytest.raises(BinanceUsdmFillUnsupported):
            event(trade(**change))
