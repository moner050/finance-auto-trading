from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.intents import PersistedOrderIntent
from autotrader.persistence.mysql.models.strategy import StrategyVersion
from autotrader.persistence.mysql.repositories.intents import OrderIntentRepository
from autotrader.persistence.mysql.repositories.strategy import StrategyRepository
from autotrader.shared.ids import new_uuid7
from autotrader.strategies.common.decisions import StrategyStatus


class _IntentSession:
    def __init__(self, existing: PersistedOrderIntent | None = None) -> None:
        self.existing = existing
        self.execute_calls = 0

    async def execute(self, statement: object) -> None:
        del statement
        self.execute_calls += 1

    async def scalar(self, statement: object) -> PersistedOrderIntent | None:
        del statement
        return self.existing


class _StrategySession:
    def __init__(self, version: StrategyVersion) -> None:
        self.version = version
        self.scalar_calls = 0
        self.flush_calls = 0

    async def scalar(self, statement: object) -> StrategyVersion:
        del statement
        self.scalar_calls += 1
        return self.version

    async def flush(self) -> None:
        self.flush_calls += 1


def _legacy_intent(*, intent_id: UUID | None = None) -> PersistedOrderIntent:
    resolved_id = intent_id if intent_id is not None else new_uuid7()
    return PersistedOrderIntent(
        id=resolved_id,
        origin_type="STRATEGY",
        idempotency_key=f"legacy:{resolved_id}",
        canonical_payload_hash=b"i" * 32,
        account_id=new_uuid7(),
        instrument_id=new_uuid7(),
        intent_type="ENTRY",
        side="BUY",
        order_style="MARKET",
        requested_quantity=Decimal("1"),
        limit_price=None,
        strategy_signal_id=None,
        legacy_strategy_link_id=resolved_id,
        protection_position_id=None,
        protection_reason_code=None,
        operator_audit_id=None,
        reconciliation_diff_id=None,
        created_at=datetime(2026, 8, 24, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_legacy_intent_cannot_reuse_another_history_link() -> None:
    session = _IntentSession()
    intent = _legacy_intent()
    intent.legacy_strategy_link_id = new_uuid7()

    with pytest.raises(ValueError, match="self-link"):
        await OrderIntentRepository(cast(AsyncSession, session)).create_or_get(intent)

    assert session.execute_calls == 0


@pytest.mark.asyncio
async def test_legacy_intent_history_cannot_be_created_after_retirement() -> None:
    session = _IntentSession()
    intent = _legacy_intent()

    with pytest.raises(ValueError, match="read-only"):
        await OrderIntentRepository(cast(AsyncSession, session)).create_or_get(intent)

    assert session.execute_calls == 0


@pytest.mark.asyncio
async def test_exact_legacy_intent_retry_remains_readable() -> None:
    existing = _legacy_intent()
    session = _IntentSession(existing)

    result = await OrderIntentRepository(cast(AsyncSession, session)).create_or_get(
        existing
    )

    assert result is existing
    assert session.execute_calls == 0


@pytest.mark.asyncio
async def test_retired_strategy_version_cannot_be_promoted_live() -> None:
    version = StrategyVersion(
        id=new_uuid7(),
        definition_id=new_uuid7(),
        version="v5.0",
        status=StrategyStatus.RETIRED,
        research_only=False,
    )
    session = _StrategySession(version)

    with pytest.raises(ValueError, match="retired"):
        await StrategyRepository(cast(AsyncSession, session)).promote_live(version.id)

    assert session.scalar_calls == 1
    assert session.flush_calls == 0
