"""Speak the dispatch protocol on behalf of the real Binance USD-M account.

`PaperBrokerSubmitter` was the only implementation of `BrokerSubmitter`, which
is why nothing connected the loop to the venue. This is its live counterpart.

The routing is one question: **does the command carry a trigger price?**

- No: an ordinary order. §31.4 - every order this strategy sends through that
  path is `MARKET`, so it fills or it does not, and there is no working state
  to cancel or replace.
- Yes: the protective stop, which is an algo order with its own service, its
  own durable record, its own protection deadline, and its own emergency
  close. `OrderStyle` cannot tell them apart - the protective order is built
  as `MARKET` too - so the trigger price is the discriminator.

The protective path needs an `EntryFill` and a `ProtectionAuthority`, and a
`BrokerOrderCommand` carries neither: no tick size, no fill price, no
protection deadline. `ProtectionContext` is that gap, named rather than
papered over, so the thing that reads fills can be built where fills live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from autotrader.domain.broker_errors import BrokerSubmissionRejected
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.binance_usdm.algo_orders import (
    BinanceUsdmProtectionRejected,
    EntryFill,
    ProtectionResult,
    binance_protection_client_algo_id,
)
from autotrader.integrations.brokers.binance_usdm.orders import BrokerWriteResult
from autotrader.risk.v6 import ProtectionAuthority


class LiveSubmissionUnsupported(RuntimeError):
    """A command shape this venue path has no way to carry.

    Not a placeholder. A market order has no working state, so a cancel of
    one is a question about something that does not exist, and answering it
    with a shrug would let the caller believe an order was withdrawn.
    """


class ProtectionRejected(BrokerSubmissionRejected):
    """Binance authoritatively refused the protective stop.

    `BinanceUsdmProtectionRejected` is a plain `RuntimeError`, so dispatch
    would file an authoritative refusal as UNKNOWN and leave the command
    looking recoverable. It is not: the venue answered.
    """


@dataclass(frozen=True, slots=True)
class ProtectionPlacement:
    """What the algo path needs and the command does not carry."""

    fill: EntryFill
    authority: ProtectionAuthority
    # Absent means this is the first stop behind the fill. Present names the
    # stop it replaces, which only a move has.
    superseded_client_algo_id: str | None = None


class ProtectionContext(Protocol):
    async def placement_for(
        self, command: BrokerOrderCommand
    ) -> ProtectionPlacement: ...


class NormalOrders(Protocol):
    async def submit_locked(self, command: BrokerOrderCommand) -> BrokerWriteResult: ...

    async def recover_by_client_id(self, client_order_id: str) -> BrokerWriteResult: ...


class Protection(Protocol):
    async def protect_first_fill(
        self, fill: EntryFill, authority: ProtectionAuthority
    ) -> ProtectionResult: ...

    async def move_stop(
        self,
        fill: EntryFill,
        authority: ProtectionAuthority,
        *,
        placement_command_id: object,
        superseded_client_algo_id: str,
    ) -> ProtectionResult: ...

    async def recover_by_client_algo_id(
        self, client_algo_id: str
    ) -> ProtectionResult: ...


@dataclass(frozen=True, slots=True)
class LiveSubmission:
    """What dispatch records: the venue's own receipt for this command."""

    broker_order_id: str


@dataclass(frozen=True, slots=True)
class LiveBrokerSubmitter:
    orders: NormalOrders = field(repr=False)
    protection: Protection = field(repr=False)
    context: ProtectionContext = field(repr=False)

    async def submit(self, command: BrokerOrderCommand) -> LiveSubmission:
        _require(command, CommandType.SUBMIT)
        if command.trigger_price is None:
            result = await self.orders.submit_locked(command)
            return LiveSubmission(broker_order_id=result.broker_order_id)
        return await self._place(command)

    async def cancel(self, command: BrokerOrderCommand) -> LiveSubmission:
        _require(command, CommandType.CANCEL)
        if command.trigger_price is None:
            raise LiveSubmissionUnsupported(
                "a Binance USD-M market order has no working state to cancel"
            )
        # A bare cancel of the stop would leave the position unprotected, and
        # nothing in this strategy asks for that. Withdrawal happens inside a
        # move, after the replacement is working.
        raise LiveSubmissionUnsupported(
            "a Binance USD-M protective stop is withdrawn by the move that "
            "replaces it, never on its own"
        )

    async def replace(self, command: BrokerOrderCommand) -> LiveSubmission:
        _require(command, CommandType.REPLACE)
        if command.trigger_price is None:
            raise LiveSubmissionUnsupported(
                "a Binance USD-M market order has no working state to replace"
            )
        return await self._place(command)

    async def recover_submit(
        self, command: BrokerOrderCommand, *, now: datetime
    ) -> LiveSubmission | None:
        del now  # The venue is the clock that matters for a recovery.
        if command.trigger_price is None:
            result = await self.orders.recover_by_client_id(
                command.broker_client_order_id
            )
            return LiveSubmission(broker_order_id=result.broker_order_id)
        placement = await self.context.placement_for(command)
        client_algo_id = _protection_client_algo_id(command, placement)
        recovered = await self.protection.recover_by_client_algo_id(client_algo_id)
        return _receipt(recovered)

    async def _place(self, command: BrokerOrderCommand) -> LiveSubmission:
        """One path for the first stop and for every move after it.

        Which one it is comes from the placement, not from the command type:
        a REPLACE whose predecessor is already gone is a first placement, and
        a SUBMIT issued while a stop is working is a move. The context reads
        that state; guessing it from the command would be guessing.
        """
        placement = await self.context.placement_for(command)
        superseded = placement.superseded_client_algo_id
        try:
            if superseded is None:
                result = await self.protection.protect_first_fill(
                    placement.fill, placement.authority
                )
            else:
                result = await self.protection.move_stop(
                    placement.fill,
                    placement.authority,
                    placement_command_id=command.id,
                    superseded_client_algo_id=superseded,
                )
        except BinanceUsdmProtectionRejected as rejected:
            raise ProtectionRejected(str(rejected)) from rejected
        return _receipt(result)


def _receipt(result: ProtectionResult) -> LiveSubmission:
    provider = result.provider_algo_id
    if provider is None:
        # The position was flattened instead of protected, so there is no
        # working order for dispatch to record. Reporting an accepted command
        # here would file a stop that does not exist.
        raise LiveSubmissionUnsupported(
            "Binance USD-M protection ended in an emergency close, not an order"
        )
    return LiveSubmission(broker_order_id=provider)


def _protection_client_algo_id(
    command: BrokerOrderCommand, placement: ProtectionPlacement
) -> str:
    # A first placement is filed under the entry's id; a move under its own.
    if placement.superseded_client_algo_id is None:
        return binance_protection_client_algo_id(placement.fill.entry_command_id)
    return binance_protection_client_algo_id(command.id)


def _require(command: BrokerOrderCommand, expected: CommandType) -> None:
    if type(command) is not BrokerOrderCommand:
        raise TypeError("Binance USD-M dispatch command must be exact")
    if command.command_type is not expected:
        raise ValueError(f"Binance USD-M dispatch expected a {expected.value} command")


__all__ = (
    "LiveBrokerSubmitter",
    "LiveSubmission",
    "LiveSubmissionUnsupported",
    "ProtectionContext",
    "ProtectionPlacement",
    "ProtectionRejected",
)
