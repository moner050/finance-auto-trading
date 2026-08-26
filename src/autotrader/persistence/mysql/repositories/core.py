from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.core import (
    CoreDataSource,
    CoreExchange,
    CoreInstrument,
    CoreMarket,
)
from autotrader.shared.ids import new_uuid7


class CoreReferenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def data_source_by_code(self, code: str) -> CoreDataSource | None:
        return await self._session.scalar(
            select(CoreDataSource).where(CoreDataSource.code == code)
        )

    async def market_by_code(self, code: str) -> CoreMarket | None:
        return await self._session.scalar(
            select(CoreMarket).where(CoreMarket.code == code)
        )

    async def exchange_by_market_code(
        self, market_id: UUID, code: str
    ) -> CoreExchange | None:
        return await self._session.scalar(
            select(CoreExchange).where(
                CoreExchange.market_id == market_id, CoreExchange.code == code
            )
        )

    async def instrument_by_exchange_code(
        self, exchange_id: UUID, code: str
    ) -> CoreInstrument | None:
        return await self._session.scalar(
            select(CoreInstrument).where(
                CoreInstrument.exchange_id == exchange_id, CoreInstrument.code == code
            )
        )


@dataclass(frozen=True, slots=True)
class InstrumentListing:
    """One instrument exactly as its venue lists it."""

    exchange_code: str
    code: str
    name: str
    instrument_type: str

    def __post_init__(self) -> None:
        for field_name, limit in (
            ("exchange_code", 32),
            ("code", 64),
            ("name", 256),
            ("instrument_type", 32),
        ):
            value = getattr(self, field_name)
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(f"{field_name} must be a trimmed, non-empty string")
            if len(value) > limit:
                raise ValueError(f"{field_name} exceeds {limit} characters")


class InstrumentNotRegisteredError(LookupError):
    """Raised when a caller names an instrument nobody registered."""


class UnknownExchangeError(LookupError):
    """Raised when a listing names an exchange that is not open for trading."""


class CoreInstrumentRegistry:
    """The write side of the canonical instrument table.

    Instruments are not seed data. A venue lists and delists them, and there
    are far too many to carry as fixed constants the way markets and exchanges
    are. What has to stay fixed is their identity: registering the same listing
    twice returns the same id, and a delisted instrument keeps the id that the
    decisions already recorded against it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register(self, listing: InstrumentListing) -> UUID:
        """Register the listing, or adopt the row that already stands for it."""
        exchange = await self._trading_exchange(listing.exchange_code)
        existing = await self._locked(exchange.id, listing.code)
        if existing is None:
            stored = CoreInstrument(
                id=new_uuid7(),
                exchange_id=exchange.id,
                code=listing.code,
                name=listing.name,
                instrument_type=listing.instrument_type,
                status="ACTIVE",
            )
            self._session.add(stored)
            await self._session.flush()
            return stored.id
        if existing.instrument_type != listing.instrument_type:
            # A venue renames an instrument; it does not turn a share into a
            # perpetual. A changed type means the code was reused for a
            # different contract, and keeping the id would silently hand it
            # every decision recorded against the old one.
            raise ValueError(
                f"{listing.exchange_code}:{listing.code} is already registered "
                f"as {existing.instrument_type}"
            )
        existing.name = listing.name
        # A relisting is the same instrument coming back, not a new one.
        existing.status = "ACTIVE"
        await self._session.flush()
        return existing.id

    async def resolve(self, exchange_code: str, code: str) -> UUID:
        """The registered id, raising rather than letting a caller invent one."""
        exchange = await self._trading_exchange(exchange_code)
        instrument = await self._session.scalar(
            select(CoreInstrument).where(
                CoreInstrument.exchange_id == exchange.id,
                CoreInstrument.code == code,
                CoreInstrument.status == "ACTIVE",
            )
        )
        if instrument is None:
            raise InstrumentNotRegisteredError(f"{exchange_code}:{code}")
        return instrument.id

    async def delist(self, exchange_code: str, code: str) -> UUID:
        """Stop trading it. Nothing is deleted; decisions still point here."""
        exchange = await self._trading_exchange(exchange_code)
        instrument = await self._locked(exchange.id, code)
        if instrument is None:
            raise InstrumentNotRegisteredError(f"{exchange_code}:{code}")
        instrument.status = "INACTIVE"
        await self._session.flush()
        return instrument.id

    async def _locked(self, exchange_id: UUID, code: str) -> CoreInstrument | None:
        return await self._session.scalar(
            select(CoreInstrument)
            .where(
                CoreInstrument.exchange_id == exchange_id,
                CoreInstrument.code == code,
            )
            .with_for_update()
        )

    async def _trading_exchange(self, code: str) -> CoreExchange:
        exchange = await self._session.scalar(
            select(CoreExchange).where(CoreExchange.code == code)
        )
        if exchange is None or exchange.status != "ACTIVE":
            # Without this a typo registers an instrument under a market that
            # nobody trades, and the loop reads an empty book forever.
            raise UnknownExchangeError(code)
        return exchange


__all__ = (
    "CoreInstrumentRegistry",
    "CoreReferenceRepository",
    "InstrumentListing",
    "InstrumentNotRegisteredError",
    "UnknownExchangeError",
)
