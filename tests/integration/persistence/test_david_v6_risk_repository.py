from __future__ import annotations

import pytest
from conftest import integration_database_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.repositories.david_v6_risk import (
    DavidV6RiskRepository,
)
from autotrader.strategies.david_v6.models import V6Market


@pytest.mark.integration
@pytest.mark.asyncio
async def test_v6_risk_repository_loads_only_exact_active_policy() -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("a MySQL connection is required for integration tests")
    engine = create_engine(Settings(database_url=url))
    session_factory = async_sessionmaker(engine)
    try:
        async with session_factory() as session:
            policy = await DavidV6RiskRepository(session).load_active_policy(
                code="DAVID_V6_BINANCE_USDM_USDT",
                market=V6Market.BINANCE_USDM,
            )
            if policy is not None:
                assert policy.market is V6Market.BINANCE_USDM
                assert policy.stream_gap_age is not None
    finally:
        await engine.dispose()
