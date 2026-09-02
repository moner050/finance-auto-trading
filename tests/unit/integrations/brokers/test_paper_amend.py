"""Cancelling and replacing a staged paper order.

Both raised `ValueError` until now, which is why the position manager could
decide to move a stop and then had nothing to move it with. A stop that has to
go to break-even is one order replacing another, and the interesting part is
the moment between them: a position with nothing behind it is worse than the
stop the move was trying to improve.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid7

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.internal_paper import (
    PaperOrderCommand,
    PaperOrderReceipt,
    PaperOrderStatus,
)
from autotrader.integrations.brokers.paper_submitter import (
    CANCELLED,
    REPLACED,
    PaperAccount,
    PaperBrokerSubmitter,
    PaperCommandNotStagedError,
)
from autotrader.strategies.david_v6.models import V6Market

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
ORDER_ID = uuid7()
ACCOUNT_ID = uuid7()
INSTRUMENT_ID = uuid7()


class _Journal:
    """A journal that records what it was asked to do, and in what order."""

    def __init__(self, staged: PaperOrderCommand | None) -> None:
        self._staged = {staged.id: staged} if staged is not None else {}
        self.receipts: list[PaperOrderReceipt] = []
        self.stagings: list[PaperOrderCommand] = []
        self.atomic: list[tuple[UUID, UUID]] = []

    async def load_receipt(self, command_id: object) -> PaperOrderReceipt | None:
        del command_id
        return None

    async def stage_command(self, command: PaperOrderCommand, digest: bytes) -> None:
        del digest
        self.stagings.append(command)

    async def persist_receipt(self, receipt: PaperOrderReceipt) -> None:
        self.receipts.append(receipt)
        self._staged.pop(receipt.command_id, None)

    async def unresolved_commands(
        self, *, order_id: UUID | None = None
    ) -> tuple[PaperOrderCommand, ...]:
        del order_id
        return tuple(self._staged.values())

    async def staged_command(self, command_id: UUID) -> PaperOrderCommand | None:
        return self._staged.get(command_id)

    async def void_and_stage(
        self,
        *,
        voided: PaperOrderReceipt,
        staged: PaperOrderCommand,
        digest: bytes,
    ) -> None:
        del digest
        self.atomic.append((voided.command_id, staged.id))
        self.receipts.append(voided)
        self.stagings.append(staged)
        self._staged.pop(voided.command_id, None)


def _account() -> PaperAccount:
    return PaperAccount(
        account_alias="internal-binance-usdm-paper",
        market=V6Market.BINANCE_USDM,
        timeframe=timedelta(minutes=5),
        fee_per_unit=Decimal("0.01"),
        slippage_per_unit=Decimal("0.02"),
    )


def _staged(command_id: UUID) -> PaperOrderCommand:
    return PaperOrderCommand(
        id=command_id,
        order_id=ORDER_ID,
        account_alias="internal-binance-usdm-paper",
        market=V6Market.BINANCE_USDM,
        side=Side.SELL,
        order_style=OrderStyle.MARKET,
        quantity=Decimal("2"),
        limit_price=None,
        signal_at=NOW,
        timeframe=timedelta(minutes=5),
        fee_per_unit=Decimal("0.01"),
        slippage_per_unit=Decimal("0.02"),
        trigger_price=Decimal("98"),
    )


def _command(
    command_type: CommandType,
    *,
    target: str | None,
    trigger: Decimal | None = Decimal("99"),
) -> BrokerOrderCommand:
    authority = {
        CommandType.CANCEL: "CANCEL",
        CommandType.REPLACE: "REPLACE_NON_INCREASING",
        CommandType.SUBMIT: "STRICT_REDUCTION",
    }[command_type]
    return BrokerOrderCommand(
        id=uuid7(),
        order_id=ORDER_ID,
        account_id=ACCOUNT_ID,
        instrument_id=INSTRUMENT_ID,
        command_type=command_type,
        target_aggregate_version=1,
        idempotency_key="k",
        command_sequence=1,
        canonical_payload_hash=b"\x00" * 32,
        broker_client_order_id="stop-1",
        target_broker_order_id=target,
        replaces_command_id=None,
        origin_type="STRATEGY",
        authority_class=authority,
        owner_runtime_instance_id=uuid7(),
        fencing_token=1,
        not_after=NOW + timedelta(minutes=5),
        side=Side.SELL,
        order_style=OrderStyle.MARKET,
        quantity=Decimal("2"),
        limit_price=None,
        time_in_force="GTC",
        trigger_price=trigger,
    )


@pytest.mark.asyncio
async def test_a_cancel_resolves_the_staged_order_as_producing_nothing() -> None:
    working = uuid7()
    journal = _Journal(_staged(working))
    submitter = PaperBrokerSubmitter(journal=journal, account=_account())

    await submitter.cancel(_command(CommandType.CANCEL, target=f"paper:{working.hex}"))

    assert len(journal.receipts) == 1
    receipt = journal.receipts[0]
    assert receipt.command_id == working
    assert receipt.status is PaperOrderStatus.NO_FILL
    assert receipt.reason_code == CANCELLED
    assert receipt.filled_quantity == Decimal(0)
    # The whole quantity is still owed, which is what a voided order means.
    assert receipt.remaining_quantity == receipt.requested_quantity


@pytest.mark.asyncio
async def test_a_replace_voids_and_stages_in_one_write() -> None:
    """Two calls cannot promise the moment between them does not exist. One
    transaction can, and that moment is a position with no stop behind it."""
    working = uuid7()
    journal = _Journal(_staged(working))
    submitter = PaperBrokerSubmitter(journal=journal, account=_account())

    await submitter.replace(
        _command(CommandType.REPLACE, target=f"paper:{working.hex}")
    )

    assert len(journal.atomic) == 1
    voided, staged = journal.atomic[0]
    assert voided == working
    assert staged != working
    # Nothing went through the single-write paths, which is the assertion.
    assert journal.stagings[-1].id == staged
    assert journal.receipts[-1].reason_code == REPLACED


@pytest.mark.asyncio
async def test_a_replace_carries_the_new_trigger() -> None:
    working = uuid7()
    journal = _Journal(_staged(working))
    submitter = PaperBrokerSubmitter(journal=journal, account=_account())

    await submitter.replace(
        _command(
            CommandType.REPLACE,
            target=f"paper:{working.hex}",
            trigger=Decimal("99.5"),
        )
    )

    assert journal.stagings[-1].trigger_price == Decimal("99.5")


@pytest.mark.asyncio
async def test_voiding_something_that_is_not_staged_is_refused() -> None:
    """Already filled, already voided, or never ours. Writing a receipt over
    any of those would overwrite an answer the broker already gave."""
    journal = _Journal(None)
    submitter = PaperBrokerSubmitter(journal=journal, account=_account())

    with pytest.raises(PaperCommandNotStagedError):
        await submitter.cancel(
            _command(CommandType.CANCEL, target=f"paper:{uuid7().hex}")
        )

    assert journal.receipts == []


@pytest.mark.asyncio
async def test_an_amendment_that_names_no_target_is_refused() -> None:
    """A cancel is its own command with its own id. Reading the target from
    that id would void nothing and report success."""
    journal = _Journal(_staged(uuid7()))
    submitter = PaperBrokerSubmitter(journal=journal, account=_account())

    with pytest.raises(ValueError, match="must name the paper order"):
        await submitter.cancel(_command(CommandType.CANCEL, target=None))


@pytest.mark.asyncio
async def test_the_command_type_has_to_match_the_call() -> None:
    """A SUBMIT arriving at cancel would void a working order and stage
    nothing in its place."""
    working = uuid7()
    submitter = PaperBrokerSubmitter(
        journal=_Journal(_staged(working)), account=_account()
    )

    with pytest.raises(ValueError, match="CANCEL"):
        await submitter.cancel(
            _command(CommandType.SUBMIT, target=f"paper:{working.hex}")
        )
    with pytest.raises(ValueError, match="REPLACE"):
        await submitter.replace(
            _command(CommandType.SUBMIT, target=f"paper:{working.hex}")
        )
