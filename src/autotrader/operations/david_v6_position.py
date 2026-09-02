from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum

from autotrader.config.settings import RuntimeMode as ExecutionMode
from autotrader.domain.enums import OrderStyle, Side
from autotrader.shared.decimal import require_decimal


class V6PositionActionKind(StrEnum):
    ACTIVATE_INITIAL_STOP = "ACTIVATE_INITIAL_STOP"
    ADD_AND_MOVE_STOP = "ADD_AND_MOVE_STOP"
    MOVE_STOP_TO_BREAK_EVEN = "MOVE_STOP_TO_BREAK_EVEN"
    RECORD_FIB_25 = "RECORD_FIB_25"
    RECORD_FIB_50_RESEARCH = "RECORD_FIB_50_RESEARCH"
    OBSERVE_PARTIAL_1_2R = "OBSERVE_PARTIAL_1_2R"
    OBSERVE_PARTIAL_1_5R = "OBSERVE_PARTIAL_1_5R"
    EXIT_FULL_FIB_66 = "EXIT_FULL_FIB_66"
    EXIT_FULL_METODO_CROSS_DOWN = "EXIT_FULL_METODO_CROSS_DOWN"
    EXIT_FULL_BLOCKING_BIG_TRADE = "EXIT_FULL_BLOCKING_BIG_TRADE"
    EXIT_FULL_SESSION_CLOSE = "EXIT_FULL_SESSION_CLOSE"
    EMERGENCY_EXIT_FULL = "EMERGENCY_EXIT_FULL"


@dataclass(frozen=True, slots=True)
class V6ManagedPosition:
    side: Side
    initial_entry_price: Decimal
    average_entry_price: Decimal
    initial_quantity: Decimal
    remaining_quantity: Decimal
    initial_stop_price: Decimal
    active_stop_price: Decimal | None
    initial_stop_active: bool
    original_approved_risk: Decimal
    current_worst_case_risk: Decimal
    add_count: int
    break_even_active: bool
    fib_25_recorded: bool
    fib_50_recorded: bool
    shadow_1_2r_recorded: bool
    shadow_1_5r_recorded: bool

    def __post_init__(self) -> None:
        if type(self.side) is not Side:
            raise TypeError("side must be an exact Side")
        for name in (
            "initial_entry_price",
            "average_entry_price",
            "initial_quantity",
            "remaining_quantity",
            "initial_stop_price",
            "original_approved_risk",
            "current_worst_case_risk",
        ):
            value = require_decimal(getattr(self, name))
            if value <= 0 and name not in {
                "original_approved_risk",
                "current_worst_case_risk",
            }:
                raise ValueError(f"{name} must be positive")
            if value < 0:
                raise ValueError(f"{name} must not be negative")
            object.__setattr__(self, name, value)
        if self.active_stop_price is not None:
            active_stop = require_decimal(self.active_stop_price)
            if active_stop <= 0:
                raise ValueError("active_stop_price must be positive")
            object.__setattr__(self, "active_stop_price", active_stop)
        if self.initial_stop_active != (self.active_stop_price is not None):
            raise ValueError("active stop state and price must agree")
        if self.add_count not in {0, 1}:
            raise ValueError("add_count must be zero or one")
        if self.current_worst_case_risk > self.original_approved_risk:
            raise ValueError("current risk cannot exceed original approved risk")
        if (
            self.side is Side.BUY
            and self.initial_stop_price >= self.initial_entry_price
        ):
            raise ValueError("long initial stop must be below entry")
        if (
            self.side is Side.SELL
            and self.initial_stop_price <= self.initial_entry_price
        ):
            raise ValueError("short initial stop must be above entry")


@dataclass(frozen=True, slots=True)
class V6PositionFacts:
    current_price: Decimal
    atr_5m: Decimal
    tick_size: Decimal
    actual_entry_fee_per_unit: Decimal
    taker_exit_fee_per_unit: Decimal
    q95_adverse_stop_slippage: Decimal
    slippage_sample_sufficient: bool
    fib_25_price: Decimal | None
    fib_50_price: Decimal | None
    fib_66_price: Decimal | None
    blocking_big_trade: bool
    metodo_exit_signal: bool
    protection_failed: bool
    # Section 7 wants a flat book before the close and forbids holding
    # overnight. The session evaluation already computes this and it was only
    # ever read as a blocker on a new entry, which does nothing about what is
    # already held.
    must_be_flat: bool = False

    def __post_init__(self) -> None:
        for name in ("current_price", "atr_5m", "tick_size"):
            value = require_decimal(getattr(self, name))
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        for name in ("fib_25_price", "fib_50_price", "fib_66_price"):
            level = getattr(self, name)
            if level is None:
                continue
            value = require_decimal(level)
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        for name in (
            "actual_entry_fee_per_unit",
            "taker_exit_fee_per_unit",
            "q95_adverse_stop_slippage",
        ):
            value = require_decimal(getattr(self, name))
            if value < 0:
                raise ValueError(f"{name} must not be negative")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class V6PositionAction:
    kind: V6PositionActionKind
    reason: str
    order_style: OrderStyle | None
    quantity: Decimal | None
    stop_price: Decimal | None
    average_entry_price: Decimal | None
    resulting_worst_case_risk: Decimal
    reduce_only: bool
    telemetry_only: bool
    account_halt: bool

    def __post_init__(self) -> None:
        """A telemetry action carries no order.

        Section 15.2 puts several items at `telemetry_only`, meaning recorded
        and never allowed to move a decision, and section 11.4 says the 25%
        and 50% retracements produce no orders at all. The flag said so and
        nothing read it: every telemetry action happened to be built with no
        quantity, so the rule held by construction and would have stopped
        holding the first time one was built any other way.
        """
        if self.telemetry_only and (
            self.order_style is not None
            or self.quantity is not None
            or self.stop_price is not None
            or self.account_halt
        ):
            raise ValueError("a telemetry-only action cannot carry an order")


def manage_v6_position(
    position: V6ManagedPosition,
    facts: V6PositionFacts,
    *,
    mode: ExecutionMode,
) -> tuple[V6PositionAction, ...]:
    if type(position) is not V6ManagedPosition:
        raise TypeError("position must be an exact V6ManagedPosition")
    if type(facts) is not V6PositionFacts:
        raise TypeError("facts must be exact V6PositionFacts")
    if type(mode) is not ExecutionMode:
        raise TypeError("mode must be an exact ExecutionMode")
    position.__post_init__()
    facts.__post_init__()

    if facts.protection_failed:
        return (_full_exit(position, V6PositionActionKind.EMERGENCY_EXIT_FULL, True),)
    if facts.must_be_flat:
        # Above the market reads and below a broken stop. A deadline cannot be
        # argued with - the position is out whatever the tape says - but a
        # position with no working protection is more urgent still.
        return (
            _full_exit(position, V6PositionActionKind.EXIT_FULL_SESSION_CLOSE, False),
        )
    if facts.blocking_big_trade:
        return (
            _full_exit(
                position,
                V6PositionActionKind.EXIT_FULL_BLOCKING_BIG_TRADE,
                False,
            ),
        )
    if facts.metodo_exit_signal:
        return (
            _full_exit(
                position,
                V6PositionActionKind.EXIT_FULL_METODO_CROSS_DOWN,
                False,
            ),
        )
    if facts.fib_66_price is not None and _reached(
        position.side, facts.current_price, facts.fib_66_price
    ):
        return (_full_exit(position, V6PositionActionKind.EXIT_FULL_FIB_66, False),)
    if not position.initial_stop_active:
        return (
            V6PositionAction(
                kind=V6PositionActionKind.ACTIVATE_INITIAL_STOP,
                reason="INITIAL_PROTECTION_REQUIRED",
                order_style=None,
                quantity=None,
                stop_price=position.initial_stop_price,
                average_entry_price=position.average_entry_price,
                resulting_worst_case_risk=position.current_worst_case_risk,
                reduce_only=False,
                telemetry_only=False,
                account_halt=False,
            ),
        )

    add = _add_action(position, facts)
    if add is not None:
        return (add,)

    actions: list[V6PositionAction] = []
    break_even = _break_even_action(position, facts)
    if break_even is not None:
        actions.append(break_even)
    if (
        not position.fib_25_recorded
        and facts.fib_25_price is not None
        and _reached(position.side, facts.current_price, facts.fib_25_price)
    ):
        actions.append(_telemetry(position, V6PositionActionKind.RECORD_FIB_25))
    if (
        not position.fib_50_recorded
        and facts.fib_50_price is not None
        and _reached(position.side, facts.current_price, facts.fib_50_price)
    ):
        actions.append(
            _telemetry(position, V6PositionActionKind.RECORD_FIB_50_RESEARCH)
        )
    if mode is ExecutionMode.SHADOW:
        favorable = _favorable_move(
            position.side,
            position.initial_entry_price,
            facts.current_price,
        )
        initial_r = abs(position.initial_entry_price - position.initial_stop_price)
        if not position.shadow_1_2r_recorded and favorable >= initial_r * Decimal(
            "1.2"
        ):
            actions.append(
                _telemetry(position, V6PositionActionKind.OBSERVE_PARTIAL_1_2R)
            )
        if not position.shadow_1_5r_recorded and favorable >= initial_r * Decimal(
            "1.5"
        ):
            actions.append(
                _telemetry(position, V6PositionActionKind.OBSERVE_PARTIAL_1_5R)
            )
    return tuple(actions)


def _add_action(
    position: V6ManagedPosition,
    facts: V6PositionFacts,
) -> V6PositionAction | None:
    if position.add_count != 0 or not facts.slippage_sample_sufficient:
        return None
    favorable = _favorable_move(
        position.side,
        position.initial_entry_price,
        facts.current_price,
    )
    threshold = max(
        position.initial_entry_price * Decimal("0.0010"),
        facts.atr_5m * Decimal("0.35"),
    )
    if favorable < threshold or favorable <= 0:
        return None
    quantity = position.initial_quantity
    total_quantity = position.remaining_quantity + quantity
    average_entry = (
        position.average_entry_price * position.remaining_quantity
        + facts.current_price * quantity
    ) / total_quantity
    proposed_stop = _break_even_stop(position.side, average_entry, facts)
    active_stop = _non_widening_stop(
        position.side,
        position.active_stop_price,
        proposed_stop,
    )
    resulting_risk = _worst_case_risk(
        position.side,
        average_entry,
        active_stop,
        total_quantity,
    )
    if resulting_risk > min(
        position.current_worst_case_risk,
        position.original_approved_risk,
    ):
        return None
    return V6PositionAction(
        kind=V6PositionActionKind.ADD_AND_MOVE_STOP,
        reason="ONE_FAVORABLE_ADD_WITH_WEIGHTED_BREAK_EVEN",
        order_style=OrderStyle.MARKET,
        quantity=quantity,
        stop_price=active_stop,
        average_entry_price=average_entry,
        resulting_worst_case_risk=resulting_risk,
        reduce_only=False,
        telemetry_only=False,
        account_halt=False,
    )


def _break_even_action(
    position: V6ManagedPosition,
    facts: V6PositionFacts,
) -> V6PositionAction | None:
    if position.break_even_active or not facts.slippage_sample_sufficient:
        return None
    initial_r = abs(position.initial_entry_price - position.initial_stop_price)
    favorable = _favorable_move(
        position.side,
        position.initial_entry_price,
        facts.current_price,
    )
    if favorable < initial_r * Decimal("0.30"):
        return None
    proposed_stop = _break_even_stop(
        position.side,
        position.average_entry_price,
        facts,
    )
    active_stop = _non_widening_stop(
        position.side,
        position.active_stop_price,
        proposed_stop,
    )
    resulting_risk = _worst_case_risk(
        position.side,
        position.average_entry_price,
        active_stop,
        position.remaining_quantity,
    )
    return V6PositionAction(
        kind=V6PositionActionKind.MOVE_STOP_TO_BREAK_EVEN,
        reason="GENERAL_BREAK_EVEN_AT_0_30R",
        order_style=None,
        quantity=None,
        stop_price=active_stop,
        average_entry_price=position.average_entry_price,
        resulting_worst_case_risk=resulting_risk,
        reduce_only=False,
        telemetry_only=False,
        account_halt=False,
    )


def _break_even_stop(
    side: Side,
    entry_price: Decimal,
    facts: V6PositionFacts,
) -> Decimal:
    raw_offset = (
        facts.actual_entry_fee_per_unit
        + facts.taker_exit_fee_per_unit
        + facts.q95_adverse_stop_slippage
        + facts.tick_size
    )
    offset = (raw_offset / facts.tick_size).to_integral_value(
        rounding=ROUND_CEILING
    ) * facts.tick_size
    return entry_price + offset if side is Side.BUY else entry_price - offset


def _non_widening_stop(
    side: Side,
    current_stop: Decimal | None,
    proposed_stop: Decimal,
) -> Decimal:
    if current_stop is None:
        return proposed_stop
    if side is Side.BUY:
        return max(current_stop, proposed_stop)
    return min(current_stop, proposed_stop)


def _worst_case_risk(
    side: Side,
    average_entry: Decimal,
    stop_price: Decimal,
    quantity: Decimal,
) -> Decimal:
    loss_per_unit = (
        average_entry - stop_price if side is Side.BUY else stop_price - average_entry
    )
    return max(loss_per_unit, Decimal(0)) * quantity


def _favorable_move(side: Side, entry: Decimal, current: Decimal) -> Decimal:
    return current - entry if side is Side.BUY else entry - current


def _reached(side: Side, current: Decimal, target: Decimal) -> bool:
    return current >= target if side is Side.BUY else current <= target


def _telemetry(
    position: V6ManagedPosition,
    kind: V6PositionActionKind,
) -> V6PositionAction:
    return V6PositionAction(
        kind=kind,
        reason=kind.value,
        order_style=None,
        quantity=None,
        stop_price=None,
        average_entry_price=None,
        resulting_worst_case_risk=position.current_worst_case_risk,
        reduce_only=False,
        telemetry_only=True,
        account_halt=False,
    )


def _full_exit(
    position: V6ManagedPosition,
    kind: V6PositionActionKind,
    account_halt: bool,
) -> V6PositionAction:
    # Section 9.3 concedes price on the way out rather than fighting for an
    # exact fill, so an exit never rests as a limit.
    return V6PositionAction(
        kind=kind,
        reason=kind.value,
        order_style=OrderStyle.MARKET,
        quantity=position.remaining_quantity,
        stop_price=None,
        average_entry_price=None,
        resulting_worst_case_risk=Decimal(0),
        reduce_only=True,
        telemetry_only=False,
        account_halt=account_halt,
    )


__all__ = (
    "ExecutionMode",
    "V6ManagedPosition",
    "V6PositionAction",
    "V6PositionActionKind",
    "V6PositionFacts",
    "manage_v6_position",
)
