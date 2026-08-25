from __future__ import annotations

import pytest

from autotrader.execution.reconciliation.gates import (
    ReconciliationRestartState,
    may_start_all_consumers,
    may_start_consumers,
)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (ReconciliationRestartState(True, True, 0, 0), True),
        (ReconciliationRestartState(False, True, 0, 0), False),
        (ReconciliationRestartState(True, False, 0, 0), False),
        (ReconciliationRestartState(True, True, 1, 0), False),
        (ReconciliationRestartState(True, True, 0, 1), False),
    ],
)
def test_restart_requires_a_complete_clean_snapshot(
    state: ReconciliationRestartState, expected: bool
) -> None:
    assert may_start_consumers(state) is expected


def test_restart_rejects_invalid_negative_counts() -> None:
    with pytest.raises(ValueError):
        may_start_consumers(ReconciliationRestartState(True, True, -1, 0))


def test_restart_rejects_any_enabled_account_without_a_clean_snapshot() -> None:
    clean = ReconciliationRestartState(True, True, 0, 0)
    incomplete = ReconciliationRestartState(True, False, 0, 0)

    assert may_start_all_consumers((clean,))
    assert not may_start_all_consumers((clean, incomplete))
    assert not may_start_all_consumers(())
