"""Turn a Binance USD-M trade into the execution the ledger records.

The paper counterpart resolves an order against a closed bar. Here the venue
has already decided: `userTrades` says what filled, at what price, for what
commission, and gives every fill an id of its own.

That id is the whole reason this is safe to run repeatedly.
`broker_execution_id` is the venue's trade id, and the ledger deduplicates on
it, so a settlement pass that overlaps the previous one adds nothing. The
alternative - trusting a time window not to overlap - would double a position
the first time a clock moved.

`source_sequence` is that same trade id. Binance issues them monotonically per
symbol, which is exactly what the execution watermark needs to say "everything
through here is accounted for" and to notice when something is missing.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from autotrader.domain.enums import Side
from autotrader.execution.fills.models import (
    BrokerExecutionEvent,
    ChargeBasis,
    ChargeEffect,
    ChargeLegRole,
    ExecutionChargeComponent,
)
from autotrader.integrations.brokers.binance_usdm.account import BinanceUsdmTradeFact
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc

SOURCE_PARTITION = "binance-usdm:BTCUSDT"
COMMISSION = "COMMISSION"
SETTLEMENT_ASSET = "USDT"


class BinanceUsdmFillUnsupported(ValueError):
    """A trade this translation refuses to guess at."""


def binance_trade_execution_id(trade_id: int) -> str:
    """The venue's own identity for one fill, in one place."""
    if type(trade_id) is not int or trade_id < 0:
        raise ValueError("Binance USD-M trade ID is invalid")
    return f"BINANCE-USDM-TRADE:{trade_id}"


def binance_execution_event(
    *,
    trade: BinanceUsdmTradeFact,
    account_id: UUID,
    instrument_id: UUID,
    broker_id: UUID,
    order_id: UUID,
    broker_order_id: str,
    broker_client_order_id: str,
    currency: str,
    leg_role: ChargeLegRole,
    observed_at: datetime,
) -> BrokerExecutionEvent:
    """The execution this trade stands for.

    `side` comes from the trade. Every fill belongs to one order - the entry
    or the protective stop - and that order already carries the side it was
    placed on, so the ledger's check that the two agree is what catches a
    trade matched to the wrong order rather than a redundant assertion.
    """
    if type(trade) is not BinanceUsdmTradeFact:
        raise TypeError("Binance USD-M trade fact must be exact")
    if trade.symbol != "BTCUSDT":
        raise BinanceUsdmFillUnsupported("Binance USD-M settlement supports BTCUSDT")
    if trade.quantity <= 0 or trade.price <= 0:
        raise BinanceUsdmFillUnsupported("a Binance USD-M fill needs size and price")

    return BrokerExecutionEvent(
        id=new_uuid7(),
        broker_id=broker_id,
        account_id=account_id,
        order_id=order_id,
        broker_order_id=broker_order_id,
        broker_client_order_id=broker_client_order_id,
        broker_execution_id=binance_trade_execution_id(trade.trade_id),
        source_partition=SOURCE_PARTITION,
        source_sequence=trade.trade_id,
        instrument_id=instrument_id,
        side=_side(trade.side),
        quantity=trade.quantity,
        price=trade.price,
        charges=_charges(trade, currency=currency, leg_role=leg_role),
        currency=currency,
        executed_at=require_utc(trade.occurred_at),
        observed_at=require_utc(observed_at),
        payload_hash=trade_hash(trade),
    )


def _side(value: str) -> Side:
    try:
        return Side(value)
    except ValueError:
        raise BinanceUsdmFillUnsupported("Binance USD-M fill side is invalid") from None


def _charges(
    trade: BinanceUsdmTradeFact, *, currency: str, leg_role: ChargeLegRole
) -> tuple[ExecutionChargeComponent, ...]:
    """The commission, and only when it is denominated in what settles.

    Binance charges in BNB when the discount is enabled, and the ledger's
    currency is a three-letter code that cannot say BNB. Recording a BNB
    commission under the settlement currency would put a wrong number in the
    ledger; dropping it would lose real money from it. So this refuses, and
    the refusal is what surfaces an account configured a way this system does
    not model.
    """
    if trade.commission <= 0:
        # Not a charge. The component refuses a zero, so a rebated fill
        # simply has no component rather than a zero row.
        return ()
    if trade.commission_asset != SETTLEMENT_ASSET:
        raise BinanceUsdmFillUnsupported(
            f"Binance USD-M commission in {trade.commission_asset} cannot be "
            f"recorded against a {currency} ledger"
        )
    return (
        ExecutionChargeComponent(
            component_ordinal=0,
            amount=trade.commission,
            currency=currency,
            charge_kind=COMMISSION,
            effect=ChargeEffect.DEBIT,
            leg_role=leg_role,
            charge_basis=ChargeBasis.PER_UNIT,
            basis_quantity=trade.quantity,
            basis_notional=None,
        ),
    )


def trade_hash(trade: BinanceUsdmTradeFact) -> bytes:
    """What the venue said, so a replay that says something else is visible."""
    payload = {
        "trade_id": trade.trade_id,
        "order_id": trade.order_id,
        "symbol": trade.symbol,
        "side": trade.side,
        "quantity": _text(trade.quantity),
        "price": _text(trade.price),
        "commission": _text(trade.commission),
        "commission_asset": trade.commission_asset,
        "realized_pnl": _text(trade.realized_pnl),
        "occurred_at": require_utc(trade.occurred_at).isoformat(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


def _text(value: Decimal) -> str:
    return format(value, "f")


__all__ = (
    "SOURCE_PARTITION",
    "BinanceUsdmFillUnsupported",
    "binance_execution_event",
    "binance_trade_execution_id",
    "trade_hash",
)
