"""Live account snapshot readers for the three brokers.

Each broker's capture already exists; what was missing was the last step —
presenting what it found in the one shape the reconciliation loop compares.
These are that step, and nothing else. They open no connections and hold no
credentials: a reader is given a callable that returns its broker's snapshot,
so the transport and the secrets stay where they already are and these stay
testable without a live account.

Order ids come from the same builders the writers use. Deriving the format a
second time here would let the two drift, and a broker order id that does not
match the one we recorded reads as an order somebody else placed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from autotrader.execution.reconciliation.models import BrokerSnapshot
from autotrader.integrations.brokers.binance_usdm.account import (
    BinanceUsdmAccountSnapshot,
)
from autotrader.integrations.brokers.binance_usdm.algo_orders import (
    binance_provider_algo_id,
)
from autotrader.integrations.brokers.binance_usdm.orders import (
    binance_provider_order_id,
)
from autotrader.integrations.brokers.kis.account_snapshot_contracts import (
    KisStableKrDomesticCashAccountSnapshot,
)
from autotrader.integrations.brokers.kis.cash_order_recovery import KisDailyOrder
from autotrader.integrations.brokers.kis.cash_writer import kis_provider_order_id
from autotrader.integrations.brokers.live_snapshots import (
    BINANCE_USDM_EXCHANGE_CODE,
    KRX_EXCHANGE_CODE,
    US_EXCHANGE_CODE,
    ReportedAccount,
    ReportedOrder,
    ReportedPosition,
    SymbolResolver,
    broker_snapshot,
)
from autotrader.integrations.brokers.toss.us_account_snapshot import (
    TossUsAccountSnapshot,
)
from autotrader.integrations.brokers.toss.us_cash_writer import toss_provider_order_id
from autotrader.integrations.brokers.toss.us_orders import TossUsOrderFact
from autotrader.shared.decimal import decimal_to_string
from autotrader.shared.time import require_utc

# How long an answer about a live account is worth acting on. Deliberately
# short: the comparison refuses a stale snapshot, and a stale refusal is a
# better outcome than sizing against a picture of a minute ago.
SNAPSHOT_WINDOW = timedelta(seconds=30)

# Toss reports a live order in these states and a finished one otherwise.
_TOSS_WORKING = frozenset({"PLACED", "PARTIALLY_FILLED", "PENDING", "ACCEPTED"})
# Binance says so directly.
_BINANCE_WORKING = frozenset({"NEW", "PARTIALLY_FILLED"})
# An algo order is either resting or it is not; there is no partial fill
# of a trigger.
_BINANCE_ALGO_WORKING = frozenset({"NEW"})


@dataclass(frozen=True, slots=True)
class SnapshotIdentity:
    """Which account, at which broker, an answer is about."""

    broker_id: UUID
    account_id: UUID


def kis_reported(
    snapshot: KisStableKrDomesticCashAccountSnapshot,
    orders: Sequence[KisDailyOrder],
) -> ReportedAccount:
    """KIS never echoes a client order id, so none is reported."""
    return ReportedAccount(
        complete=True,
        positions=tuple(
            ReportedPosition(symbol=position.symbol, quantity=position.total_quantity)
            for position in snapshot.positions
        ),
        open_orders=tuple(
            ReportedOrder(
                broker_order_id=kis_provider_order_id(
                    order.order_date, order.organization_number, order.order_number
                ),
                broker_client_order_id=None,
                terms={
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "style": order.order_style.value,
                    "quantity": _amount(order.order_quantity),
                    "remaining": _amount(order.remaining_quantity),
                    "limit_price": (
                        "" if order.limit_price is None else _amount(order.limit_price)
                    ),
                },
            )
            for order in orders
            if order.remaining_quantity > 0
        ),
    )


def toss_reported(
    snapshot: TossUsAccountSnapshot,
    orders: Sequence[TossUsOrderFact],
) -> ReportedAccount:
    """Toss's order list omits the client order id it accepted on submission."""
    return ReportedAccount(
        complete=True,
        positions=tuple(
            ReportedPosition(symbol=position.symbol, quantity=position.total_quantity)
            for position in snapshot.positions
        ),
        open_orders=tuple(
            ReportedOrder(
                broker_order_id=toss_provider_order_id(order.provider_order_id),
                broker_client_order_id=None,
                terms={
                    "symbol": order.symbol,
                    "side": order.side,
                    "state": order.state,
                    "quantity": _amount(order.quantity),
                    "filled": _amount(order.cumulative_fill_quantity),
                    "limit_price": (
                        "" if order.limit_price is None else _amount(order.limit_price)
                    ),
                },
            )
            for order in orders
            if order.state in _TOSS_WORKING
        ),
    )


def binance_reported(snapshot: BinanceUsdmAccountSnapshot) -> ReportedAccount:
    """Binance carries our client order id back, so it is reported.

    It also lists every symbol it has ever margined, most at zero. Those are
    not positions and are dropped when the snapshot is assembled.

    **The algo orders are here too.** The protective stop is an algo order and
    nothing else, so reporting only the normal ones would show every protected
    position as holding an order the venue does not have - drift on every
    position that is behaving correctly, which teaches an operator to ignore
    the thing reconciliation exists to say.
    """
    return ReportedAccount(
        complete=True,
        positions=tuple(
            ReportedPosition(symbol=position.symbol, quantity=position.amount)
            for position in snapshot.positions
        ),
        open_orders=tuple(
            ReportedOrder(
                broker_order_id=binance_provider_order_id(order.order_id),
                broker_client_order_id=order.client_order_id,
                terms={
                    "symbol": order.symbol,
                    "side": order.side,
                    "type": order.order_type,
                    "status": order.status,
                    "quantity": _amount(order.original_quantity),
                    "executed": _amount(order.executed_quantity),
                    "reduce_only": str(order.reduce_only),
                },
            )
            for order in snapshot.normal_orders
            if order.status in _BINANCE_WORKING
        )
        + tuple(
            ReportedOrder(
                broker_order_id=binance_provider_algo_id(order.algo_id),
                broker_client_order_id=order.client_algo_id,
                terms={
                    "symbol": order.symbol,
                    "side": order.side,
                    "type": order.order_type,
                    "status": order.status,
                    "quantity": _amount(order.quantity),
                    "trigger_price": _amount(order.trigger_price),
                    "close_position": str(order.close_position),
                },
            )
            for order in snapshot.algo_orders
            if order.status in _BINANCE_ALGO_WORKING
        ),
    )


class LiveSnapshotReader:
    """One broker's live answer, as a `BrokerSnapshotReader`.

    The capture is a callable rather than a transport so that credentials and
    connections stay with the adapters that already own them, and so this can
    be exercised against a recorded answer.
    """

    def __init__(
        self,
        *,
        identity: SnapshotIdentity,
        exchange_code: str,
        capture: Callable[[datetime], Awaitable[ReportedAccount]],
        resolver: SymbolResolver,
        window: timedelta = SNAPSHOT_WINDOW,
    ) -> None:
        self._identity = identity
        self._exchange_code = exchange_code
        self._capture = capture
        self._resolver = resolver
        self._window = window

    async def read_snapshot(
        self, *, account_id: object, now: datetime
    ) -> BrokerSnapshot:
        moment = require_utc(now)
        if account_id != self._identity.account_id:
            # A reader answers for the account it was built for. Answering for
            # another would attribute one account's holdings to another.
            raise ValueError("this reader answers for one account only")
        reported = await self._capture(moment)
        return await broker_snapshot(
            reported,
            broker_id=self._identity.broker_id,
            account_id=self._identity.account_id,
            exchange_code=self._exchange_code,
            resolver=self._resolver,
            now=moment,
            window=self._window,
        )


def kis_reader(
    *,
    identity: SnapshotIdentity,
    capture: Callable[[datetime], Awaitable[ReportedAccount]],
    resolver: SymbolResolver,
    window: timedelta = SNAPSHOT_WINDOW,
) -> LiveSnapshotReader:
    return LiveSnapshotReader(
        identity=identity,
        exchange_code=KRX_EXCHANGE_CODE,
        capture=capture,
        resolver=resolver,
        window=window,
    )


def toss_reader(
    *,
    identity: SnapshotIdentity,
    capture: Callable[[datetime], Awaitable[ReportedAccount]],
    resolver: SymbolResolver,
    window: timedelta = SNAPSHOT_WINDOW,
) -> LiveSnapshotReader:
    return LiveSnapshotReader(
        identity=identity,
        exchange_code=US_EXCHANGE_CODE,
        capture=capture,
        resolver=resolver,
        window=window,
    )


def binance_reader(
    *,
    identity: SnapshotIdentity,
    capture: Callable[[datetime], Awaitable[ReportedAccount]],
    resolver: SymbolResolver,
    window: timedelta = SNAPSHOT_WINDOW,
) -> LiveSnapshotReader:
    return LiveSnapshotReader(
        identity=identity,
        exchange_code=BINANCE_USDM_EXCHANGE_CODE,
        capture=capture,
        resolver=resolver,
        window=window,
    )


def _amount(value: Decimal) -> str:
    return decimal_to_string(value)


__all__ = (
    "SNAPSHOT_WINDOW",
    "LiveSnapshotReader",
    "SnapshotIdentity",
    "binance_reader",
    "binance_reported",
    "kis_reader",
    "kis_reported",
    "toss_reader",
    "toss_reported",
)
