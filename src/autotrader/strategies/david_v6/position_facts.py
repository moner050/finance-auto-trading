"""What the position manager needs to see, taken from one assembly.

`manage_v6_position` wants a `V6PositionFacts`. Every field of it is already
computed somewhere in the evidence the tick assembles for the entry decision:
the fibonacci levels come off the HLIT setup, the blocking big trade off the
order flow, the exit signal off Método, and the costs off the same schedule
the risk request prices a trade against.

Built from that one assembly rather than measured again, so the manager and
the entry decision see the same market. Two assemblies on one pass would be
two chances to disagree, and a position closed against one reading while a new
entry was refused against another is a session nobody can reconstruct.

What that buys is consistency and what it costs is cadence: the position is
looked at once per closed bar, like everything else here. The author watches
a screen and reacts inside the bar. This system is bar-driven throughout, and
making the exit path alone sub-bar would give it a different clock from the
stop that protects it.
"""

from __future__ import annotations

from decimal import Decimal

from autotrader.domain.enums import Side
from autotrader.operations.david_v6_position import V6PositionFacts
from autotrader.strategies.david_v6.costs import FeeSchedule
from autotrader.strategies.david_v6.evidence import V6EvidenceBundle
from autotrader.strategies.david_v6.hlit import HlitSetup
from autotrader.strategies.david_v6.metodo import MetodoFacts
from autotrader.strategies.david_v6.models import EvidenceState
from autotrader.strategies.david_v6.order_flow import (
    BigTradesUnmeasured,
    OrderFlowFacts,
    blocking_big_trade_ahead,
)


def position_facts(
    bundle: V6EvidenceBundle,
    *,
    side: Side,
    setup: HlitSetup | None,
    current_price: Decimal,
    atr_5m: Decimal,
    tick_size: Decimal,
    fee_schedule: FeeSchedule,
    stop_slippage_q95: Decimal | None,
    protection_failed: bool,
) -> V6PositionFacts:
    """One pass's view of an open position's world.

    Absent evidence reads as "no signal" rather than as a reason to act. A
    missing order-flow reading is not a blocking big trade, and a missing
    Método reading is not an exit: acting on evidence that was never taken
    would close a position because a measurement failed.

    The one exception is the slippage sample, which has its own flag. There
    the manager is told the sample is insufficient rather than handed a
    number, because a stop distance modelled on too few bars is a number that
    looks measured and is not.
    """
    entry_fee = fee_schedule.entry_fee_per_unit
    exit_fee = fee_schedule.exit_taker_fee_per_unit
    return V6PositionFacts(
        current_price=current_price,
        atr_5m=atr_5m,
        tick_size=tick_size,
        actual_entry_fee_per_unit=Decimal(0) if entry_fee is None else entry_fee,
        taker_exit_fee_per_unit=Decimal(0) if exit_fee is None else exit_fee,
        q95_adverse_stop_slippage=(
            Decimal(0) if stop_slippage_q95 is None else stop_slippage_q95
        ),
        slippage_sample_sufficient=stop_slippage_q95 is not None,
        fib_25_price=None if setup is None else setup.fib_25,
        fib_50_price=None if setup is None else setup.fib_50,
        fib_66_price=None if setup is None else setup.fib_66,
        blocking_big_trade=_blocking(bundle, side=side, price=current_price),
        metodo_exit_signal=_metodo_exit(bundle, side=side),
        protection_failed=protection_failed,
    )


def _blocking(bundle: V6EvidenceBundle, *, side: Side, price: Decimal) -> bool:
    """An opposing Big Trade standing in the direction of travel.

    A window that could not rank its events raises rather than answering,
    deliberately, so the caller decides what it means. Here it means do not
    exit: closing a position because a measurement failed acts on evidence
    that was never taken, and the position is not unprotected while we wait -
    its structural stop is behind it either way.

    That is the opposite of the answer the entry path gives the same
    condition, and it should be. Refusing to open on no information costs a
    trade; closing on no information spends one.
    """
    item = bundle.order_flow
    if item.state is not EvidenceState.AVAILABLE:
        return False
    value = item.value
    if type(value) is not OrderFlowFacts:
        return False
    try:
        return blocking_big_trade_ahead(value, side=side, reference_price=price)
    except BigTradesUnmeasured:
        return False


def _metodo_exit(bundle: V6EvidenceBundle, *, side: Side) -> bool:
    """The Método cross against the position.

    A long is exited when the fast average crosses down through the slow one,
    a short when it crosses up. Reading only the down-cross would have left
    every short holding through its own exit signal - the same one-sided
    wiring that made every evaluation a BUY.
    """
    item = bundle.metodo
    if item.state is not EvidenceState.AVAILABLE:
        return False
    value = item.value
    if type(value) is not MetodoFacts:
        return False
    return value.sma_6_70_cross_down if side is Side.BUY else value.sma_6_70_cross_up


__all__ = ("position_facts",)
