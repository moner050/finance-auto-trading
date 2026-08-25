from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from autotrader.execution.fills.completeness import (
    ExecutionCompletenessProof,
    is_terminal_window_complete,
)
from autotrader.execution.orders.models import (
    BrokerOrderLinkState,
    all_exposure_links_terminal,
)


@dataclass(frozen=True, slots=True)
class TerminalReleaseDecision:
    release: bool
    reason: str | None


def decide_terminal_release(
    *,
    links: tuple[BrokerOrderLinkState, ...],
    proof: ExecutionCompletenessProof | None,
    broker_client_order_ids: frozenset[str],
    first_possible_acceptance_at: datetime,
    terminal_at: datetime,
    now: datetime,
) -> TerminalReleaseDecision:
    exposure_links = tuple(link for link in links if link.exposure_bearing)
    if not exposure_links or not all_exposure_links_terminal(links):
        return TerminalReleaseDecision(False, "LIVE_EXPOSURE_LINK")
    if not is_terminal_window_complete(
        proof=proof,
        broker_order_ids=frozenset(link.broker_order_id for link in exposure_links),
        broker_client_order_ids=broker_client_order_ids,
        first_possible_acceptance_at=first_possible_acceptance_at,
        terminal_at=terminal_at,
        now=now,
    ):
        return TerminalReleaseDecision(False, "TERMINAL_RELEASE_PENDING")
    return TerminalReleaseDecision(True, None)
