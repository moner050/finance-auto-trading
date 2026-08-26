"""Turn a settled paper order into the execution the ledger records.

The paper broker resolves an order against a closed bar and writes a receipt.
Nothing carried that receipt any further, so `exec_position` stayed empty no
matter how many paper orders filled — and every guarantee that reads from the
position ledger, reconciliation drift and protective-stop enforcement among
them, had nothing to read.

This is the translation, and only the translation. What it must not do is
invent anything the receipt does not say: a no-fill moved nothing, so it is
not an execution at all.
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
from autotrader.integrations.brokers.internal_paper import (
    PaperOrderReceipt,
    PaperOrderStatus,
)
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc

SOURCE_PARTITION = "internal-paper"
FEE = "FEE"
SLIPPAGE = "SLIPPAGE"

_MOVED = frozenset({PaperOrderStatus.FILLED, PaperOrderStatus.PARTIALLY_FILLED})


def paper_broker_order_id(command_id: UUID) -> str:
    """The identity the paper submitter reports, in one place."""
    return f"paper:{command_id.hex}"


def paper_execution_event(
    *,
    receipt: PaperOrderReceipt,
    account_id: UUID,
    instrument_id: UUID,
    broker_id: UUID,
    broker_client_order_id: str,
    side: Side,
    currency: str,
    leg_role: ChargeLegRole,
    observed_at: datetime,
) -> BrokerExecutionEvent | None:
    """The execution this receipt stands for, or None when nothing moved."""
    moment = require_utc(observed_at)
    if receipt.status not in _MOVED:
        return None
    if receipt.filled_quantity <= 0 or receipt.fill_price is None:
        # A status that claims a fill without a price or a quantity is not a
        # partial fill, it is a broken receipt, and guessing would put a
        # number in the ledger that no bar ever printed.
        raise ValueError("a filled paper receipt must carry a price and a quantity")
    if receipt.filled_at is None:
        raise ValueError("a filled paper receipt must carry a fill time")
    return BrokerExecutionEvent(
        id=new_uuid7(),
        broker_id=broker_id,
        account_id=account_id,
        order_id=receipt.order_id,
        broker_order_id=paper_broker_order_id(receipt.command_id),
        broker_client_order_id=broker_client_order_id,
        # Stable, because the ledger deduplicates on it and a second
        # settlement pass must not double the position.
        broker_execution_id=paper_broker_order_id(receipt.command_id),
        source_partition=SOURCE_PARTITION,
        source_sequence=None,
        instrument_id=instrument_id,
        side=side,
        quantity=receipt.filled_quantity,
        price=receipt.fill_price,
        charges=_charges(receipt, currency=currency, leg_role=leg_role),
        currency=currency,
        executed_at=require_utc(receipt.filled_at),
        observed_at=moment,
        payload_hash=receipt_hash(receipt),
    )


def _charges(
    receipt: PaperOrderReceipt, *, currency: str, leg_role: ChargeLegRole
) -> tuple[ExecutionChargeComponent, ...]:
    """Fees and slippage, keeping the ordinals contiguous.

    A charge of zero is not a charge, and the component refuses one, so a
    fee-free account simply has fewer components rather than a zero row.
    """
    charged = (
        (FEE, receipt.fee),
        (SLIPPAGE, receipt.slippage_cost),
    )
    return tuple(
        ExecutionChargeComponent(
            component_ordinal=ordinal,
            amount=amount,
            currency=currency,
            charge_kind=kind,
            effect=ChargeEffect.DEBIT,
            leg_role=leg_role,
            charge_basis=ChargeBasis.PER_UNIT,
            basis_quantity=receipt.filled_quantity,
            basis_notional=None,
        )
        for ordinal, (kind, amount) in enumerate(
            pair for pair in charged if pair[1] > 0
        )
    )


def receipt_hash(receipt: PaperOrderReceipt) -> bytes:
    """What the ledger compares when the same execution arrives twice."""
    payload = {
        "command_id": receipt.command_id.hex,
        "order_id": receipt.order_id.hex,
        "status": receipt.status.value,
        "filled_quantity": _plain(receipt.filled_quantity),
        "remaining_quantity": _plain(receipt.remaining_quantity),
        "fill_price": _plain(receipt.fill_price),
        "fee": _plain(receipt.fee),
        "slippage_cost": _plain(receipt.slippage_cost),
        "filled_at": (
            None if receipt.filled_at is None else receipt.filled_at.isoformat()
        ),
        "reason_code": receipt.reason_code,
        "command_digest": receipt.command_digest.hex(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).digest()


def _plain(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


__all__ = (
    "FEE",
    "SLIPPAGE",
    "SOURCE_PARTITION",
    "paper_broker_order_id",
    "paper_execution_event",
    "receipt_hash",
)
