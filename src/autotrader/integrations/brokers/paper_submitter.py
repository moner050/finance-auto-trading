"""Speak the dispatch protocol on behalf of the internal paper broker.

A paper order fills on the bar after the one that produced the signal, and
that bar has not closed when the order is sent. So sending only stages the
command; a later pass resolves the fill once the bar exists. Pretending the
fill is known at send time would be look-ahead, which is the one thing the
paper broker exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol, cast
from uuid import UUID

from autotrader.domain.enums import OrderStyle
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.internal_paper import (
    PAPER_ACCOUNT_BINDINGS,
    InternalPaperBroker,
    PaperExecutionBar,
    PaperOrderCommand,
    PaperOrderReceipt,
    PaperOrderStatus,
)
from autotrader.shared.decimal import require_decimal
from autotrader.shared.time import require_utc
from autotrader.strategies.david_v6.models import V6Market

STAGED = "STAGED"


class PaperJournal(Protocol):
    async def load_receipt(self, command_id: object) -> PaperOrderReceipt | None: ...

    async def stage_command(
        self, command: PaperOrderCommand, digest: bytes
    ) -> None: ...

    async def persist_receipt(self, receipt: PaperOrderReceipt) -> None: ...

    async def unresolved_commands(
        self, *, order_id: UUID | None = None
    ) -> tuple[PaperOrderCommand, ...]: ...


class ExecutionBars(Protocol):
    async def bar_at(self, command: PaperOrderCommand) -> PaperExecutionBar | None:
        """The bar that resolves this command, once it has closed."""
        ...


@dataclass(frozen=True, slots=True)
class PaperAccount:
    """What the paper broker needs that an order command does not carry."""

    account_alias: str
    market: V6Market
    timeframe: timedelta
    fee_per_unit: Decimal
    slippage_per_unit: Decimal

    def __post_init__(self) -> None:
        if type(cast(object, self.market)) is not V6Market:
            raise TypeError("market must be an exact V6Market")
        # The command refuses an alias that does not belong to this market,
        # and dispatch turns anything a broker raises into UNKNOWN. Checking
        # here means a misconfigured account fails at wiring, where it reads
        # as the configuration error it is, rather than as a broker timeout.
        if PAPER_ACCOUNT_BINDINGS.get(self.account_alias) is not self.market:
            raise ValueError(
                f"{self.account_alias!r} is not the paper account for "
                f"{self.market.value}"
            )
        if type(self.timeframe) is not timedelta or self.timeframe <= timedelta(0):
            raise ValueError("timeframe must be positive")
        for name in ("fee_per_unit", "slippage_per_unit"):
            value = require_decimal(getattr(self, name))
            if value < 0:
                raise ValueError(f"{name} must not be negative")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class StagedSubmission:
    """What dispatch records: the order reached the broker and is pending."""

    broker_order_id: str


class PaperBrokerSubmitter:
    """Adapts the internal paper broker to the dispatch protocol."""

    def __init__(self, *, journal: PaperJournal, account: PaperAccount) -> None:
        self._journal = journal
        self._account = account

    async def submit(self, command: BrokerOrderCommand) -> StagedSubmission:
        if command.command_type is not CommandType.SUBMIT:
            raise ValueError("the paper broker only accepts SUBMIT commands")
        paper = self._paper_command(command)
        await self._journal.stage_command(paper, paper.command_digest())
        return StagedSubmission(broker_order_id=f"paper:{paper.id.hex}")

    async def cancel(self, command: BrokerOrderCommand) -> StagedSubmission:
        del command
        raise ValueError("the paper broker cannot cancel a staged order")

    async def replace(self, command: BrokerOrderCommand) -> StagedSubmission:
        del command
        raise ValueError("the paper broker cannot replace a staged order")

    async def recover_submit(
        self, command: BrokerOrderCommand, *, now: datetime
    ) -> StagedSubmission | None:
        """A staged command is already durable, so recovery just re-reads it."""
        del now
        paper = self._paper_command(command)
        receipt = await self._journal.load_receipt(paper.id)
        staged = await self._journal.unresolved_commands(order_id=command.order_id)
        if receipt is None and not staged:
            return None
        return StagedSubmission(broker_order_id=f"paper:{paper.id.hex}")

    def _paper_command(self, command: BrokerOrderCommand) -> PaperOrderCommand:
        # The signal is the bar before the one that fills it, so the command
        # names that bar and the broker insists the fill bar follows it exactly.
        signal_at = require_utc(command.not_after) - self._account.timeframe
        return PaperOrderCommand(
            id=command.id,
            order_id=command.order_id,
            account_alias=self._account.account_alias,
            market=self._account.market,
            side=command.side,
            order_style=command.order_style,
            quantity=command.quantity,
            limit_price=(
                command.limit_price if command.order_style is OrderStyle.LIMIT else None
            ),
            signal_at=signal_at,
            timeframe=self._account.timeframe,
            fee_per_unit=self._account.fee_per_unit,
            slippage_per_unit=self._account.slippage_per_unit,
        )


async def resolve_paper_fills(
    *,
    broker: InternalPaperBroker,
    journal: PaperJournal,
    bars: ExecutionBars,
    order_id: UUID | None = None,
) -> tuple[PaperOrderReceipt, ...]:
    """Settle every staged command whose fill bar has now closed.

    The bar is checked first on purpose. Handing the broker a command whose
    bar has not arrived would have it write a permanent no-fill for a missing
    bar, which is a different thing from a bar that simply has not closed yet.
    When the bar is there the broker does the filling, so the fill rule stays
    in one place.
    """
    resolved: list[PaperOrderReceipt] = []
    for command in await journal.unresolved_commands(order_id=order_id):
        if await bars.bar_at(command) is None:
            continue
        resolved.append(await broker.submit(command))
    return tuple(resolved)


__all__ = (
    "STAGED",
    "ExecutionBars",
    "PaperAccount",
    "PaperBrokerSubmitter",
    "PaperJournal",
    "PaperOrderStatus",
    "StagedSubmission",
    "resolve_paper_fills",
)
