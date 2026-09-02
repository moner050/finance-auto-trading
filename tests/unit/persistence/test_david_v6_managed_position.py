"""Describing an open position as the manager needs to see it.

`manage_v6_position` was written, tested, and never called, so nothing had
ever had to answer where its `V6ManagedPosition` comes from. Most of it is
already stored; these are the parts that are arithmetic over what is stored,
and each one changes what the manager then does.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from autotrader.domain.enums import Side
from autotrader.persistence.mysql.repositories.david_v6_position import (
    FIB_25,
    PARTIAL_1_5R,
    ManagedPositionUnavailableError,
    OpeningEntry,
    managed_position_from,
)

ENTRY = Decimal("100")
STOP = Decimal("98")


def _position(**changes: object):
    values: dict[str, object] = {
        "side": Side.BUY,
        "average_cost": ENTRY,
        "entries": (OpeningEntry(quantity=Decimal("2"), average_price=ENTRY),),
        "remaining_quantity": Decimal("2"),
        "structural_stop": STOP,
        "active_stop": None,
        "marks": frozenset(),
    }
    values.update(changes)
    return managed_position_from(**values)  # type: ignore[arg-type]


def _entry(quantity: str, price: str = "100") -> OpeningEntry:
    return OpeningEntry(quantity=Decimal(quantity), average_price=Decimal(price))


def test_every_entry_order_after_the_first_is_an_add() -> None:
    """An order, not a lot. A lot is written per fill, so one entry that
    filled in three pieces leaves three lots and no adds at all - and the
    domain allows at most one add, so miscounting them does not size a trade
    wrongly, it makes the position unmanageable."""
    assert _position(entries=(_entry("2"),)).add_count == 0
    assert _position(entries=(_entry("2"), _entry("1"))).add_count == 1


def test_the_initial_quantity_is_the_first_lot_not_what_is_held() -> None:
    """R is measured against the size the position opened with. Reading it
    from the remainder would shrink R every time a part was closed."""
    position = _position(
        entries=(_entry("2"), _entry("1")),
        remaining_quantity=Decimal("1"),
    )

    assert position.initial_quantity == Decimal("2")
    assert position.remaining_quantity == Decimal("1")


def test_approved_risk_is_the_opening_lot_against_the_structural_stop() -> None:
    assert _position().original_approved_risk == Decimal("4")


def test_worst_case_risk_follows_the_working_stop_once_there_is_one() -> None:
    """Before a stop is working the exposure is measured against the stop the
    decision named; after, against the one actually behind the position."""
    naked = _position(active_stop=None)
    protected = _position(active_stop=Decimal("99"))

    assert naked.current_worst_case_risk == Decimal("4")
    assert protected.current_worst_case_risk == Decimal("2")


def test_a_position_with_no_stop_working_is_not_protected() -> None:
    """`initial_stop_active` says whether one is working, not whether one was
    intended, because the manager's first act is to put it on."""
    assert _position(active_stop=None).initial_stop_active is False
    assert _position(active_stop=Decimal("99")).initial_stop_active is True


def test_break_even_is_read_from_where_the_stop_sits() -> None:
    """Read rather than stored, so a stop moved by hand at the venue is
    reflected instead of contradicted."""
    assert _position(active_stop=Decimal("99")).break_even_active is False
    assert _position(active_stop=ENTRY).break_even_active is True
    assert _position(active_stop=Decimal("101")).break_even_active is True


def test_break_even_reverses_for_a_short() -> None:
    """A short reaches break-even when its stop comes *down* to the entry.
    The comparison that is right for a long is exactly backwards here, and
    getting it wrong would report a losing stop as protected."""
    short = {
        "side": Side.SELL,
        "average_cost": ENTRY,
        "structural_stop": Decimal("102"),
    }

    assert _position(**short, active_stop=Decimal("101")).break_even_active is False
    assert _position(**short, active_stop=ENTRY).break_even_active is True
    assert _position(**short, active_stop=Decimal("99")).break_even_active is True


def test_no_stop_is_not_break_even() -> None:
    assert _position(active_stop=None).break_even_active is False


def test_marks_gate_the_observations_that_have_already_been_emitted() -> None:
    position = _position(marks=frozenset({FIB_25, PARTIAL_1_5R}))

    assert position.fib_25_recorded is True
    assert position.fib_50_recorded is False
    assert position.shadow_1_2r_recorded is False
    assert position.shadow_1_5r_recorded is True


def test_a_position_with_no_entry_is_refused() -> None:
    """There is no entry price to measure R from, and inventing one would put
    a risk figure on record that no fill supports."""
    with pytest.raises(ManagedPositionUnavailableError, match="no entry"):
        _position(entries=())
