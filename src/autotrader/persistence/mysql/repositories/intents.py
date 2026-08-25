from __future__ import annotations

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.persistence.mysql.models.intents import PersistedOrderIntent


class IntentIdentityCollisionError(ValueError):
    pass


class OrderIntentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_or_get(self, intent: PersistedOrderIntent) -> PersistedOrderIntent:
        if intent.legacy_strategy_link_id is not None:
            if intent.legacy_strategy_link_id != intent.id:
                raise ValueError("legacy strategy intent requires an exact self-link")
            existing = await self._find_by_idempotency_key(intent.idempotency_key)
            if existing is None:
                raise ValueError("legacy strategy intent history is read-only")
            self._require_same_identity(existing, intent)
            return existing

        await self._session.execute(
            insert(PersistedOrderIntent)
            .values(
                id=intent.id,
                origin_type=intent.origin_type,
                idempotency_key=intent.idempotency_key,
                canonical_payload_hash=intent.canonical_payload_hash,
                account_id=intent.account_id,
                instrument_id=intent.instrument_id,
                intent_type=intent.intent_type,
                side=intent.side,
                order_style=intent.order_style,
                requested_quantity=intent.requested_quantity,
                limit_price=intent.limit_price,
                strategy_signal_id=intent.strategy_signal_id,
                legacy_strategy_link_id=intent.legacy_strategy_link_id,
                protection_position_id=intent.protection_position_id,
                protection_reason_code=intent.protection_reason_code,
                operator_audit_id=intent.operator_audit_id,
                reconciliation_diff_id=intent.reconciliation_diff_id,
                created_at=intent.created_at,
            )
            .prefix_with("IGNORE")
        )
        existing = await self._find_by_idempotency_key(intent.idempotency_key)
        if existing is None:
            raise RuntimeError("inserted order intent cannot be read")
        self._require_same_identity(existing, intent)
        return existing

    async def _find_by_idempotency_key(
        self, idempotency_key: str
    ) -> PersistedOrderIntent | None:
        return await self._session.scalar(
            select(PersistedOrderIntent)
            .where(PersistedOrderIntent.idempotency_key == idempotency_key)
            .with_for_update()
        )

    @staticmethod
    def _require_same_identity(
        existing: PersistedOrderIntent, intent: PersistedOrderIntent
    ) -> None:
        if (
            existing.canonical_payload_hash != intent.canonical_payload_hash
            or existing.strategy_signal_id != intent.strategy_signal_id
            or existing.legacy_strategy_link_id != intent.legacy_strategy_link_id
        ):
            raise IntentIdentityCollisionError(
                "order intent identity payload collision"
            )
