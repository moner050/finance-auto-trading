"""The commands this loop builds have to be ones the venue path accepts.

Binance USD-M requires the client order id to name the command: recovery
reconstructs the id from the command alone, so an id naming a decision or an
intent cannot be reconstructed and the adapter refuses it. Both
`_load_authority` and `_validate_recovery_command` check it, which means
*every* order the loop dispatched would have been refused - before a request
was built, before anything reached the venue.

Nothing connected the two ends. The loop chose the id and the adapter checked
it, and neither knew about the other, so nothing failed until a real command
was validated. This is that connection.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid7

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.intents.models import IntentOrigin
from autotrader.execution.orders.models import CommandType, Order, OrderStatus
from autotrader.execution.orders.service import (
    NEW_EXPOSURE,
    STRICT_REDUCTION,
    OrderCommandFactory,
    OrderSubmissionContext,
)
from autotrader.integrations.brokers.binance_usdm.orders import (
    _client_order_id,
    _validate_recovery_command,
    binance_normal_client_order_id,
)
from autotrader.integrations.brokers.common import BrokerWriteDisabled

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _order(client_order_id: str, *, trigger: Decimal | None = None) -> Order:
    return Order(
        id=uuid7(),
        order_intent_id=uuid7(),
        risk_decision_id=uuid7(),
        account_id=uuid7(),
        instrument_id=uuid7(),
        side=Side.BUY,
        order_style=OrderStyle.MARKET,
        requested_quantity=Decimal("0.002"),
        limit_price=None,
        status=OrderStatus.CREATED,
        aggregate_version=1,
        broker_client_order_id=client_order_id,
        created_at=NOW,
        trigger_price=trigger,
    )


def _submission(
    command_id: UUID, *, authority: str = NEW_EXPOSURE
) -> OrderSubmissionContext:
    return OrderSubmissionContext(
        broker_client_order_id=binance_normal_client_order_id(command_id),
        owner_runtime_instance_id=uuid7(),
        fencing_token=9,
        not_after=NOW + timedelta(minutes=2),
        time_in_force="NONE",
        authority_class=authority,
        created_at=NOW,
        command_id=command_id,
    )


@pytest.mark.parametrize("authority", (NEW_EXPOSURE, STRICT_REDUCTION))
def test_the_command_carries_the_id_the_venue_can_reconstruct(
    authority: str,
) -> None:
    for _ in range(32):
        command_id = uuid7()
        submission = _submission(command_id, authority=authority)
        command = OrderCommandFactory().create(
            order=_order(submission.broker_client_order_id),
            command_type=CommandType.SUBMIT,
            submission=submission,
            origin=IntentOrigin.STRATEGY,
        )
        assert command.id == command_id
        assert command.broker_client_order_id == binance_normal_client_order_id(
            command.id
        )
        _client_order_id(command.broker_client_order_id)


def test_the_adapter_accepts_a_command_this_loop_builds() -> None:
    """The check that used to refuse every order, run against one.

    Three things had to agree before this could pass, and none of them did:
    the client order id had to name the command, and `origin_type` and
    `authority_class` had to be values something actually mints. The adapters
    were checking for two strings - "DAVID_V6_DECISION" and
    "V6_PROVIDER_WRITE" - that nothing in this system produces.
    """
    for authority in (NEW_EXPOSURE, STRICT_REDUCTION):
        command_id = uuid7()
        submission = _submission(command_id, authority=authority)
        command = OrderCommandFactory().create(
            order=_order(submission.broker_client_order_id),
            command_type=CommandType.SUBMIT,
            submission=submission,
            origin=IntentOrigin.STRATEGY,
        )
        _validate_recovery_command(replace(command, dispatch_attempted_at=NOW))


def test_an_origin_the_loop_never_sends_to_a_venue_is_refused() -> None:
    """Operator and reconciliation origins exist, and nothing in this loop
    dispatches them. An adapter that took one would execute something no
    strategy decision produced."""
    command_id = uuid7()
    submission = _submission(command_id)
    command = OrderCommandFactory().create(
        order=_order(submission.broker_client_order_id),
        command_type=CommandType.SUBMIT,
        submission=submission,
        origin=IntentOrigin.OPERATOR,
    )
    with pytest.raises(BrokerWriteDisabled):
        _validate_recovery_command(replace(command, dispatch_attempted_at=NOW))


def test_an_id_naming_anything_else_is_refused() -> None:
    """What the loop used to do: name the decision, or the intent."""
    command_id = uuid7()
    submission = OrderSubmissionContext(
        broker_client_order_id=f"v6-{uuid7().hex}",
        owner_runtime_instance_id=uuid7(),
        fencing_token=9,
        not_after=NOW + timedelta(minutes=2),
        time_in_force="NONE",
        authority_class=NEW_EXPOSURE,
        created_at=NOW,
        command_id=command_id,
    )
    command = OrderCommandFactory().create(
        order=_order(submission.broker_client_order_id),
        command_type=CommandType.SUBMIT,
        submission=submission,
        origin=IntentOrigin.STRATEGY,
    )
    assert command.broker_client_order_id != binance_normal_client_order_id(command.id)
    with pytest.raises(BrokerWriteDisabled):
        _validate_recovery_command(replace(command, dispatch_attempted_at=NOW))


def test_without_a_chosen_id_the_factory_still_picks_one() -> None:
    """Venues that do not care leave it absent, and nothing changes."""
    submission = OrderSubmissionContext(
        broker_client_order_id="kis-whatever",
        owner_runtime_instance_id=uuid7(),
        fencing_token=9,
        not_after=NOW + timedelta(minutes=2),
        time_in_force="NONE",
        authority_class=NEW_EXPOSURE,
        created_at=NOW,
    )
    first = OrderCommandFactory().create(
        order=_order("kis-whatever"),
        command_type=CommandType.SUBMIT,
        submission=submission,
        origin=IntentOrigin.STRATEGY,
    )
    second = OrderCommandFactory().create(
        order=_order("kis-whatever"),
        command_type=CommandType.SUBMIT,
        submission=submission,
        origin=IntentOrigin.STRATEGY,
    )
    assert first.id != second.id
