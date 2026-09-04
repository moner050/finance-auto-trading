"""The authority a Binance USD-M order is written under.

`BinanceUsdmOrderService` refuses to send anything without one, and its
`_AUTHORITY_MAX_AGE` is thirty seconds - so this cannot be filled from a
stored reconciliation fact, and should not be. Sending an order on a
five-minute-old belief about the account's leverage is sending it on a belief.

The venue is read on a cycle rather than per order. A refresh takes three
round trips, and doing that inside a decision would put a third of a minute
of network between deciding and sending. The cache is stamped with when it
was read, and the adapter refuses anything older than its window, so a cache
that stops refreshing stops orders rather than ageing quietly into one.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.trader.composition import ExecutionAccount
from autotrader.apps.trader.quotes import QuoteSource
from autotrader.domain.enums import IntentType
from autotrader.execution.orders.models import BrokerOrderCommand
from autotrader.integrations.brokers.binance_usdm.configuration import (
    ConfigurationReport,
)
from autotrader.integrations.brokers.binance_usdm.orders import (
    BinanceUsdmNormalOrderAuthority,
    BinanceUsdmOrderRole,
    BinanceUsdmSymbolFilters,
)
from autotrader.integrations.brokers.common import (
    CLOSING_AUTHORITY,
    BrokerWriteDisabled,
)
from autotrader.persistence.mysql.models.bindings import ProviderAccountBinding
from autotrader.persistence.mysql.models.intents import PersistedOrderIntent
from autotrader.persistence.mysql.models.orders import PersistedOrder
from autotrader.persistence.mysql.models.positions import Position
from autotrader.shared.time import require_utc

STRATEGY_VERSION = "david-trullas-v6.0"
SYMBOL = "BTCUSDT"


@dataclass(frozen=True, slots=True)
class VenueConfiguration:
    """One reading of what the venue says this account is set to."""

    report: ConfigurationReport
    filters: BinanceUsdmSymbolFilters
    read_at: datetime


class ConfigurationSource(Protocol):
    async def read(self) -> VenueConfiguration: ...


class ConfigurationCache:
    """The venue's configuration, re-read on a cycle and never invented.

    `current` returns the last reading whatever its age. Deciding what is too
    old belongs to the adapter, which has the window and refuses past it; a
    cache that returned nothing when stale would turn one refusal into two
    different ones with different messages.
    """

    def __init__(self, *, source: ConfigurationSource, every: timedelta) -> None:
        if every <= timedelta(0):
            raise ValueError("a configuration refresh interval must be positive")
        self._source = source
        self._every = every
        self._current: VenueConfiguration | None = None
        self._lock = asyncio.Lock()

    @property
    def current(self) -> VenueConfiguration | None:
        return self._current

    async def refresh(self, now: datetime) -> VenueConfiguration | None:
        """Re-read if the last reading is older than the interval."""
        moment = require_utc(now)
        held = self._current
        if held is not None and moment - held.read_at < self._every:
            return held
        async with self._lock:
            # Another caller may have refreshed while this one waited.
            held = self._current
            if held is not None and moment - held.read_at < self._every:
                return held
            self._current = await self._source.read()
            return self._current


@dataclass(frozen=True, slots=True)
class MySqlOrderAuthority:
    """`BinanceUsdmOrderAuthoritySource` over the loop's tables and the cache."""

    sessions: async_sessionmaker[AsyncSession]
    account: ExecutionAccount
    configuration: ConfigurationCache
    quotes: QuoteSource
    expected_leverage: int

    async def load(
        self, command: BrokerOrderCommand
    ) -> BinanceUsdmNormalOrderAuthority:
        configuration = self.configuration.current
        if configuration is None:
            # Never read means nothing is known about the account's leverage
            # or margin mode, and an order sent on that is sent on nothing.
            raise BrokerWriteDisabled(
                "Binance USD-M configuration has not been read yet"
            )
        report = configuration.report
        if not report.ready:
            raise BrokerWriteDisabled(
                "Binance USD-M configuration is not ready: "
                + ", ".join(report.blockers)
            )

        async with self.sessions() as session:
            binding = await self._binding(session)
            role, reduce_quantity = await self._role(session, command)

        quote = await self.quotes.quote()
        reference = quote.ask if command.side.value == "BUY" else quote.bid
        return BinanceUsdmNormalOrderAuthority(
            command_id=command.id,
            account_id=command.account_id,
            instrument_id=command.instrument_id,
            binding_id=binding[0],
            binding_generation=binding[1],
            policy_version_id=self.account.policy_version_id,
            strategy_version=STRATEGY_VERSION,
            # The venue's own answer about whether this key may trade, rather
            # than a flag of ours that says we think it may.
            writer_capability=report.can_trade,
            account_enabled=self.account.account.enabled,
            binding_active=True,
            # An order exists, so the intent it came from is settled: the
            # loop creates one from the other and never the other way.
            intent_locked=True,
            symbol=SYMBOL,
            role=role,
            side=command.side,
            order_style=command.order_style,
            quantity=command.quantity,
            limit_price=command.limit_price,
            expected_leverage=self.expected_leverage,
            verified_leverage=report.leverage,
            leverage_verified_at=configuration.read_at,
            position_mode=report.position_mode,
            margin_type=report.margin_type,
            auto_add_margin=report.auto_add_margin,
            filters=configuration.filters,
            notional_reference_price=reference,
            authorized_reduce_quantity=reduce_quantity,
        )

    async def _binding(self, session: AsyncSession) -> tuple[UUID, int]:
        row = (
            await session.execute(
                select(ProviderAccountBinding.id, ProviderAccountBinding.revision)
                .where(
                    ProviderAccountBinding.account_id == self.account.account.id,
                    ProviderAccountBinding.provider_code == "BINANCE",
                    ProviderAccountBinding.active.is_(True),
                )
                .order_by(ProviderAccountBinding.revision.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            raise BrokerWriteDisabled(
                "this account has no active Binance USD-M binding"
            )
        return row[0], row[1]

    async def _role(
        self, session: AsyncSession, command: BrokerOrderCommand
    ) -> tuple[BinanceUsdmOrderRole, Decimal]:
        """What this order is for, and how much it is allowed to close.

        An entry carries no reduce authority at all - the adapter refuses one
        that does - and a close must cover the whole position, so the number
        is read rather than taken from the command it is checking.
        """
        order = await session.get(PersistedOrder, command.order_id)
        if order is None:
            raise BrokerWriteDisabled("this command has no canonical order")
        intent = await session.get(PersistedOrderIntent, order.order_intent_id)
        if intent is None:
            raise BrokerWriteDisabled("this command has no intent")
        if command.authority_class != CLOSING_AUTHORITY:
            return _opening_role(IntentType(intent.intent_type)), Decimal(0)
        position = await session.scalar(
            select(Position.quantity).where(
                Position.account_id == command.account_id,
                Position.instrument_id == command.instrument_id,
            )
        )
        if position is None:
            raise BrokerWriteDisabled("there is no position for this close to cover")
        return _closing_role(intent.protection_reason_code), abs(position)


def _opening_role(intent_type: IntentType) -> BinanceUsdmOrderRole:
    if intent_type is IntentType.ENTRY:
        return BinanceUsdmOrderRole.ENTRY
    # The only other order that opens exposure is the add at thirty points.
    return BinanceUsdmOrderRole.ADD


def _closing_role(reason_code: str | None) -> BinanceUsdmOrderRole:
    """Which kind of close, from the reason the loop recorded.

    The role decides nothing the adapter checks about a close beyond that it
    does not open exposure, but it is what an operator reads later to see why
    a position left.
    """
    if reason_code == "EMERGENCY_CLOSE":
        return BinanceUsdmOrderRole.EMERGENCY_CLOSE
    if reason_code == "EXIT_FULL_SESSION_CLOSE":
        return BinanceUsdmOrderRole.SESSION_CLOSE
    return BinanceUsdmOrderRole.RISK_CLOSE


__all__ = (
    "STRATEGY_VERSION",
    "SYMBOL",
    "ConfigurationCache",
    "ConfigurationSource",
    "MySqlOrderAuthority",
    "VenueConfiguration",
)
