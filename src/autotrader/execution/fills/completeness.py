from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ExecutionCompletenessProof:
    broker_order_ids: frozenset[str]
    broker_client_order_ids: frozenset[str]
    covered_from_at: datetime | None
    covered_through_at: datetime | None
    pagination_complete: bool
    has_gap: bool
    expires_at: datetime


def is_terminal_window_complete(
    *,
    proof: ExecutionCompletenessProof | None,
    broker_order_ids: frozenset[str],
    broker_client_order_ids: frozenset[str],
    first_possible_acceptance_at: datetime,
    terminal_at: datetime,
    now: datetime,
) -> bool:
    if proof is None or proof.has_gap or not proof.pagination_complete:
        return False
    if proof.expires_at <= now:
        return False
    if proof.covered_from_at is None or proof.covered_through_at is None:
        return False
    if (
        proof.covered_from_at > first_possible_acceptance_at
        or proof.covered_through_at < terminal_at
    ):
        return False
    if not broker_order_ids <= proof.broker_order_ids:
        return False
    return broker_client_order_ids <= proof.broker_client_order_ids
