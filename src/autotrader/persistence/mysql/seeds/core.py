from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from autotrader.persistence.mysql.models.core import (
    CoreBase,
    CoreDataSource,
    CoreExchange,
    CoreMarket,
)
from autotrader.persistence.mysql.unit_of_work import SqlAlchemyUnitOfWork

SYSTEM_SOURCE_ID = UUID("01989400-0000-7000-8000-000000000010")
KR_MARKET_ID = UUID("01989400-0000-7000-8000-000000000011")
US_MARKET_ID = UUID("01989400-0000-7000-8000-000000000012")
KRX_EXCHANGE_ID = UUID("01989400-0000-7000-8000-000000000013")
NYSE_EXCHANGE_ID = UUID("01989400-0000-7000-8000-000000000014")
CRYPTO_MARKET_ID = UUID("019d0000-0000-7000-8000-000000000001")
BINANCE_USDM_EXCHANGE_ID = UUID("019d0000-0000-7000-8000-000000000002")

CRYPTO_MARKET_CODE = "CRYPTO"
BINANCE_USDM_EXCHANGE_CODE = "BINANCE_USDM"


async def seed_core_reference(uow: SqlAlchemyUnitOfWork) -> None:
    await seed_core_reference_session(uow.session)


async def seed_core_reference_session(session: AsyncSession) -> None:
    await ensure_exact_seed_row(
        session,
        CoreDataSource,
        {
            "id": SYSTEM_SOURCE_ID,
            "code": "SYSTEM",
            "name": "System",
            "status": "ACTIVE",
        },
        (CoreDataSource.code == "SYSTEM",),
    )
    await ensure_exact_seed_row(
        session,
        CoreMarket,
        {"id": KR_MARKET_ID, "code": "KR", "name": "Korea", "status": "ACTIVE"},
        (CoreMarket.code == "KR",),
    )
    await ensure_exact_seed_row(
        session,
        CoreMarket,
        {"id": US_MARKET_ID, "code": "US", "name": "United States", "status": "ACTIVE"},
        (CoreMarket.code == "US",),
    )
    await ensure_exact_seed_row(
        session,
        CoreMarket,
        {
            "id": CRYPTO_MARKET_ID,
            "code": CRYPTO_MARKET_CODE,
            "name": "Crypto",
            "status": "ACTIVE",
        },
        (CoreMarket.code == CRYPTO_MARKET_CODE,),
    )
    await ensure_exact_seed_row(
        session,
        CoreExchange,
        {
            "id": KRX_EXCHANGE_ID,
            "market_id": KR_MARKET_ID,
            "code": "KRX",
            "name": "KRX",
            "status": "ACTIVE",
        },
        (CoreExchange.market_id == KR_MARKET_ID, CoreExchange.code == "KRX"),
    )
    await ensure_exact_seed_row(
        session,
        CoreExchange,
        {
            "id": NYSE_EXCHANGE_ID,
            "market_id": US_MARKET_ID,
            "code": "NYSE",
            "name": "NYSE",
            "status": "ACTIVE",
        },
        (CoreExchange.market_id == US_MARKET_ID, CoreExchange.code == "NYSE"),
    )
    await ensure_exact_seed_row(
        session,
        CoreExchange,
        {
            "id": BINANCE_USDM_EXCHANGE_ID,
            "market_id": CRYPTO_MARKET_ID,
            "code": BINANCE_USDM_EXCHANGE_CODE,
            "name": "Binance USD-M Futures",
            "status": "ACTIVE",
        },
        (
            CoreExchange.market_id == CRYPTO_MARKET_ID,
            CoreExchange.code == BINANCE_USDM_EXCHANGE_CODE,
        ),
    )


async def ensure_exact_seed_row[SeedModel: CoreBase](
    session: AsyncSession,
    model: type[SeedModel],
    values: Mapping[str, object],
    natural_identity: tuple[ColumnElement[bool], ...],
) -> SeedModel:
    identifier = values.get("id")
    if not isinstance(identifier, UUID) or identifier.version != 7:
        raise ValueError("seed identity must be UUIDv7")
    existing = await session.get(model, identifier, with_for_update=True)
    if existing is not None:
        _require_exact_values(existing, values)
        return existing
    collision = await session.scalar(
        select(model).where(*natural_identity).with_for_update()
    )
    if collision is not None:
        raise ValueError(f"{model.__name__} natural identity is occupied")
    stored = model(**values)
    session.add(stored)
    await session.flush()
    return stored


def _require_exact_values(row: object, values: Mapping[str, object]) -> None:
    if any(getattr(row, key) != value for key, value in values.items()):
        raise ValueError(f"{type(row).__name__} seed payload conflicts")
