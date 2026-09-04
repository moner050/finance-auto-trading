"""What a live broker says an account holds, in the form the loop compares.

Each broker answers in its own shape and names instruments by its own symbol.
The reconciliation port wants one shape and our instrument ids, so this is
where the translation happens — once, rather than three times with three
chances to get the same rule slightly different.

Two rules carry most of the weight.

A flat instrument is absent, not zero. `HeldPosition` refuses a zero quantity
so that "holds nothing" and "was not asked" stay different answers. Binance's
v3 position endpoint already returns only symbols with a position or an open
order, so the drop below is usually a no-op - it was written against v2, which
reported a row for every symbol the account had ever margined, and it stays
because a broker that answers the older way must not become a flat account.

An unrecognised symbol is a refusal, not an omission. Skipping it would report
an account flat in an instrument the broker says it holds, and the comparison
would find no drift — the exact failure reconciliation exists to catch. A
snapshot that cannot be fully mapped is not a partial snapshot; it is a
snapshot we cannot read.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from autotrader.execution.reconciliation.models import (
    BrokerOpenOrder,
    BrokerSnapshot,
    HeldPosition,
)
from autotrader.shared.decimal import require_decimal
from autotrader.shared.time import require_utc

KRX_EXCHANGE_CODE = "KRX"
US_EXCHANGE_CODE = "NYSE"
BINANCE_USDM_EXCHANGE_CODE = "BINANCE_USDM"


class UnmappedInstrumentError(LookupError):
    """Raised when a broker reports a symbol we have not registered.

    Deliberately fatal to the snapshot. The alternative is a reconciliation
    run that quietly ignores part of the account.
    """


class SymbolResolver(Protocol):
    async def resolve(self, exchange_code: str, code: str) -> UUID: ...


@dataclass(frozen=True, slots=True)
class ReportedPosition:
    """One instrument as the broker named it."""

    symbol: str
    quantity: Decimal

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip():
            raise ValueError("symbol must be trimmed and non-empty")
        object.__setattr__(self, "quantity", require_decimal(self.quantity))


@dataclass(frozen=True, slots=True)
class ReportedOrder:
    """One working order as the broker described it.

    `terms` is whatever the broker said about the order, as strings. It is not
    compared against anything — the comparison keys on the order id — but it
    goes into the run hash, so a run stops looking identical when the terms
    behind an unchanged order id move.
    """

    broker_order_id: str
    broker_client_order_id: str | None
    terms: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.broker_order_id:
            raise ValueError("broker_order_id is required")
        if self.broker_client_order_id == "":
            raise ValueError("broker_client_order_id is absent or a value")


@dataclass(frozen=True, slots=True)
class ReportedAccount:
    """One broker's answer, before its symbols mean anything to us."""

    complete: bool
    positions: tuple[ReportedPosition, ...]
    open_orders: tuple[ReportedOrder, ...]


def terms_hash(terms: Mapping[str, str]) -> bytes:
    return sha256(
        json.dumps(dict(sorted(terms.items())), separators=(",", ":")).encode("utf-8")
    ).digest()


async def broker_snapshot(
    reported: ReportedAccount,
    *,
    broker_id: UUID,
    account_id: UUID,
    exchange_code: str,
    resolver: SymbolResolver,
    now: datetime,
    window: timedelta,
) -> BrokerSnapshot:
    """One broker's answer, in the shape the comparison reads."""
    moment = require_utc(now)
    if window <= timedelta(0):
        raise ValueError("a snapshot window must be positive")
    return BrokerSnapshot(
        broker_id=broker_id,
        account_id=account_id,
        complete=reported.complete,
        expires_at=moment + window,
        open_orders=tuple(
            BrokerOpenOrder(
                broker_order_id=order.broker_order_id,
                broker_client_order_id=order.broker_client_order_id,
                canonical_terms_hash=terms_hash(order.terms),
            )
            for order in reported.open_orders
        ),
        positions=await _held(
            reported.positions, exchange_code=exchange_code, resolver=resolver
        ),
    )


async def _held(
    reported: Sequence[ReportedPosition],
    *,
    exchange_code: str,
    resolver: SymbolResolver,
) -> tuple[HeldPosition, ...]:
    held: list[HeldPosition] = []
    for position in reported:
        if position.quantity == 0:
            continue
        try:
            instrument_id = await resolver.resolve(exchange_code, position.symbol)
        except Exception as error:
            raise UnmappedInstrumentError(
                f"{exchange_code}:{position.symbol} is not a registered instrument"
            ) from error
        held.append(
            HeldPosition(instrument_id=instrument_id, quantity=position.quantity)
        )
    return tuple(held)


__all__ = (
    "BINANCE_USDM_EXCHANGE_CODE",
    "KRX_EXCHANGE_CODE",
    "US_EXCHANGE_CODE",
    "ReportedAccount",
    "ReportedOrder",
    "ReportedPosition",
    "SymbolResolver",
    "UnmappedInstrumentError",
    "broker_snapshot",
    "terms_hash",
)
