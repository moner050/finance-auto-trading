from __future__ import annotations

from datetime import UTC, datetime, timedelta

from autotrader.execution.fills.completeness import (
    ExecutionCompletenessProof,
    is_terminal_window_complete,
)

NOW = datetime(2026, 8, 9, tzinfo=UTC)


def proof(**changes: object) -> ExecutionCompletenessProof:
    values: dict[str, object] = {
        "broker_order_ids": frozenset({"broker-1"}),
        "broker_client_order_ids": frozenset({"client-1"}),
        "covered_from_at": NOW - timedelta(minutes=2),
        "covered_through_at": NOW + timedelta(minutes=2),
        "pagination_complete": True,
        "has_gap": False,
        "expires_at": NOW + timedelta(minutes=2),
    }
    values.update(changes)
    return ExecutionCompletenessProof(**values)  # type: ignore[arg-type]


def test_complete_fresh_scoped_window_allows_terminal_release() -> None:
    assert is_terminal_window_complete(
        proof=proof(),
        broker_order_ids=frozenset({"broker-1"}),
        broker_client_order_ids=frozenset({"client-1"}),
        first_possible_acceptance_at=NOW - timedelta(minutes=1),
        terminal_at=NOW + timedelta(minutes=1),
        now=NOW,
    )


def test_gap_or_missing_client_scope_fails_closed() -> None:
    for candidate in (
        proof(has_gap=True),
        proof(broker_client_order_ids=frozenset()),
        proof(expires_at=NOW),
    ):
        assert not is_terminal_window_complete(
            proof=candidate,
            broker_order_ids=frozenset({"broker-1"}),
            broker_client_order_ids=frozenset({"client-1"}),
            first_possible_acceptance_at=NOW - timedelta(minutes=1),
            terminal_at=NOW + timedelta(minutes=1),
            now=NOW,
        )
