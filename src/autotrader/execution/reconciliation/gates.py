from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReconciliationRestartState:
    latest_run_succeeded: bool
    latest_run_complete: bool
    blocking_diff_count: int
    unknown_order_count: int


def may_start_consumers(state: ReconciliationRestartState) -> bool:
    """Restart only after a complete clean reconciliation; ambiguity stays stopped."""

    if state.blocking_diff_count < 0 or state.unknown_order_count < 0:
        raise ValueError("reconciliation counts must be non-negative")
    return (
        state.latest_run_succeeded
        and state.latest_run_complete
        and state.blocking_diff_count == 0
        and state.unknown_order_count == 0
    )


def may_start_all_consumers(states: tuple[ReconciliationRestartState, ...]) -> bool:
    """An enabled account without a clean snapshot is a startup failure."""

    return bool(states) and all(may_start_consumers(state) for state in states)
