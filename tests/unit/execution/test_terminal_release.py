from __future__ import annotations

from datetime import UTC, datetime, timedelta

from autotrader.execution.fills.completeness import ExecutionCompletenessProof
from autotrader.execution.fills.terminal_release import decide_terminal_release
from autotrader.execution.orders.models import BrokerOrderLinkState, OrderStatus
from autotrader.shared.ids import new_uuid7

NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


def _link(*, status: OrderStatus) -> BrokerOrderLinkState:
    return BrokerOrderLinkState(
        id=new_uuid7(),
        broker_order_id="broker-order-1",
        link_sequence=1,
        exposure_bearing=True,
        status=status,
    )


def _proof(**overrides: object) -> ExecutionCompletenessProof:
    values: dict[str, object] = {
        "broker_order_ids": frozenset({"broker-order-1"}),
        "broker_client_order_ids": frozenset({"client-order-1"}),
        "covered_from_at": NOW - timedelta(minutes=2),
        "covered_through_at": NOW,
        "pagination_complete": True,
        "has_gap": False,
        "expires_at": NOW + timedelta(minutes=1),
    }
    values.update(overrides)
    return ExecutionCompletenessProof(**values)  # type: ignore[arg-type]


def test_terminal_release_requires_terminal_lineage_and_fresh_complete_proof() -> None:
    decision = decide_terminal_release(
        links=(_link(status=OrderStatus.CANCELED),),
        proof=_proof(),
        broker_client_order_ids=frozenset({"client-order-1"}),
        first_possible_acceptance_at=NOW - timedelta(minutes=1),
        terminal_at=NOW,
        now=NOW,
    )

    assert decision.release is True
    assert decision.reason is None


def test_terminal_release_stays_pending_for_a_live_link_or_incomplete_proof() -> None:
    live = decide_terminal_release(
        links=(_link(status=OrderStatus.ACKNOWLEDGED),),
        proof=_proof(),
        broker_client_order_ids=frozenset({"client-order-1"}),
        first_possible_acceptance_at=NOW - timedelta(minutes=1),
        terminal_at=NOW,
        now=NOW,
    )
    incomplete = decide_terminal_release(
        links=(_link(status=OrderStatus.CANCELED),),
        proof=_proof(has_gap=True),
        broker_client_order_ids=frozenset({"client-order-1"}),
        first_possible_acceptance_at=NOW - timedelta(minutes=1),
        terminal_at=NOW,
        now=NOW,
    )

    assert live.release is False
    assert live.reason == "LIVE_EXPOSURE_LINK"
    assert incomplete.release is False
    assert incomplete.reason == "TERMINAL_RELEASE_PENDING"


def test_terminal_release_requires_at_least_one_exposure_bearing_link() -> None:
    decision = decide_terminal_release(
        links=(
            BrokerOrderLinkState(
                id=new_uuid7(),
                broker_order_id="non-exposure-link",
                link_sequence=1,
                exposure_bearing=False,
                status=OrderStatus.CANCELED,
            ),
        ),
        proof=_proof(broker_order_ids=frozenset({"non-exposure-link"})),
        broker_client_order_ids=frozenset({"client-order-1"}),
        first_possible_acceptance_at=NOW - timedelta(minutes=1),
        terminal_at=NOW,
        now=NOW,
    )

    assert decision.release is False
    assert decision.reason == "LIVE_EXPOSURE_LINK"
