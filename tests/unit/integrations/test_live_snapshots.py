"""Reading a live account without lying about it.

The comparison downstream is only as honest as what it is handed. Two ways a
reader could quietly report drift 0 on an account that has drifted: drop a
symbol it does not recognise, or report a flat instrument as held-zero. Both
are pinned here.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from autotrader.integrations.brokers.live_snapshots import (
    KRX_EXCHANGE_CODE,
    ReportedAccount,
    ReportedOrder,
    ReportedPosition,
    UnmappedInstrumentError,
    broker_snapshot,
    terms_hash,
)
from autotrader.shared.ids import new_uuid7

NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
WINDOW = timedelta(seconds=30)
BROKER_ID = new_uuid7()
ACCOUNT_ID = new_uuid7()


class _Registry:
    """Resolves the symbols it was told about, and refuses the rest."""

    def __init__(self, known: dict[str, UUID]) -> None:
        self._known = known

    async def resolve(self, exchange_code: str, code: str) -> UUID:
        del exchange_code
        if code not in self._known:
            raise LookupError(code)
        return self._known[code]


def _assemble(
    reported: ReportedAccount, *, known: dict[str, UUID] | None = None
) -> object:
    return asyncio.run(
        broker_snapshot(
            reported,
            broker_id=BROKER_ID,
            account_id=ACCOUNT_ID,
            exchange_code=KRX_EXCHANGE_CODE,
            resolver=_Registry({} if known is None else known),
            now=NOW,
            window=WINDOW,
        )
    )


def _account(
    positions: tuple[ReportedPosition, ...] = (),
    open_orders: tuple[ReportedOrder, ...] = (),
    *,
    complete: bool = True,
) -> ReportedAccount:
    return ReportedAccount(
        complete=complete, positions=positions, open_orders=open_orders
    )


def test_a_symbol_we_do_not_know_stops_the_snapshot() -> None:
    """Skipping it would report the account flat in something the broker says
    it holds, and the comparison would find nothing wrong."""
    reported = _account((ReportedPosition(symbol="005930", quantity=Decimal("10")),))

    with pytest.raises(UnmappedInstrumentError, match="005930"):
        _assemble(reported)


def test_a_flat_instrument_is_absent_rather_than_zero() -> None:
    known = {"005930": new_uuid7()}
    reported = _account(
        (
            ReportedPosition(symbol="005930", quantity=Decimal("0")),
            ReportedPosition(symbol="000660", quantity=Decimal("0")),
        )
    )

    snapshot = _assemble(reported, known=known)

    # And crucially it did not need to resolve either symbol, because neither
    # is a position. A zero row for a delisted symbol must not fail the read.
    assert snapshot.positions == ()  # type: ignore[attr-defined]


def test_a_held_position_carries_the_registered_instrument() -> None:
    instrument_id = new_uuid7()
    reported = _account((ReportedPosition(symbol="005930", quantity=Decimal("10")),))

    snapshot = _assemble(reported, known={"005930": instrument_id})

    held = snapshot.positions  # type: ignore[attr-defined]
    assert len(held) == 1
    assert held[0].instrument_id == instrument_id
    assert held[0].quantity == Decimal("10")


def test_a_short_position_keeps_its_sign() -> None:
    """Binance reports a short as a negative amount, and a short that reads as
    a long is worse than no answer."""
    instrument_id = new_uuid7()
    reported = _account((ReportedPosition(symbol="005930", quantity=Decimal("-3")),))

    snapshot = _assemble(reported, known={"005930": instrument_id})

    assert snapshot.positions[0].quantity == Decimal("-3")  # type: ignore[attr-defined]


def test_an_incomplete_answer_is_carried_through() -> None:
    snapshot = _assemble(_account(complete=False))

    # The comparison turns this into a blocking diff; hiding it here would
    # make a partial read look like a clean one.
    assert snapshot.complete is False  # type: ignore[attr-defined]


def test_the_snapshot_expires() -> None:
    snapshot = _assemble(_account())

    assert snapshot.expires_at == NOW + WINDOW  # type: ignore[attr-defined]


def test_a_window_that_never_expires_is_refused() -> None:
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(
            broker_snapshot(
                _account(),
                broker_id=BROKER_ID,
                account_id=ACCOUNT_ID,
                exchange_code=KRX_EXCHANGE_CODE,
                resolver=_Registry({}),
                now=NOW,
                window=timedelta(0),
            )
        )


def test_an_order_without_a_client_id_is_reported_as_absent() -> None:
    """KIS has no such field. Absent is a fact; an empty string would compare
    unequal to every client id we ever sent."""
    reported = _account(
        open_orders=(
            ReportedOrder(
                broker_order_id="KIS-KRX:20260827:00001:0000000001",
                broker_client_order_id=None,
                terms={"symbol": "005930"},
            ),
        )
    )

    snapshot = _assemble(reported)

    assert snapshot.open_orders[0].broker_client_order_id is None  # type: ignore[attr-defined]


def test_an_empty_client_id_is_refused() -> None:
    with pytest.raises(ValueError, match="absent or a value"):
        ReportedOrder(broker_order_id="X:1", broker_client_order_id="", terms={})


def test_terms_hash_does_not_depend_on_key_order() -> None:
    assert terms_hash({"a": "1", "b": "2"}) == terms_hash({"b": "2", "a": "1"})


def test_terms_hash_moves_when_a_term_moves() -> None:
    """An unchanged order id with changed terms must not hash the same, or a
    run would look identical to the one before it."""
    assert terms_hash({"quantity": "10"}) != terms_hash({"quantity": "11"})


def test_two_rows_for_one_instrument_are_refused() -> None:
    """A broker that reports an instrument twice has not told us what is
    held, and summing them would be a guess."""
    instrument_id = new_uuid7()
    reported = _account(
        (
            ReportedPosition(symbol="005930", quantity=Decimal("10")),
            ReportedPosition(symbol="005930", quantity=Decimal("4")),
        )
    )

    with pytest.raises(ValueError, match="each instrument once"):
        _assemble(reported, known={"005930": instrument_id})
