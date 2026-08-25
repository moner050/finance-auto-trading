from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from autotrader.execution.fills.completeness import ExecutionCompletenessProof
from autotrader.persistence.mysql.models.fills import (
    PersistedExecutionCheckpointScope,
    PersistedExecutionGap,
    PersistedExecutionWatermark,
    PersistedFill,
)
from autotrader.shared.ids import new_uuid7


class ExecutionWatermarkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def advance_stream_sequence(
        self,
        *,
        broker_id: UUID,
        account_id: UUID,
        source_partition: str,
        source_sequence: int,
        now: datetime,
        expires_at: datetime,
        evidence_hash: bytes,
    ) -> PersistedExecutionWatermark:
        watermark = await self._session.scalar(
            select(PersistedExecutionWatermark)
            .where(
                PersistedExecutionWatermark.broker_id == broker_id,
                PersistedExecutionWatermark.account_id == account_id,
                PersistedExecutionWatermark.source_partition == source_partition,
            )
            .with_for_update()
        )
        if watermark is None:
            watermark = PersistedExecutionWatermark(
                id=new_uuid7(),
                broker_id=broker_id,
                account_id=account_id,
                source_partition=source_partition,
                contiguous_through_sequence=source_sequence
                if source_sequence == 1
                else None,
                has_gap=source_sequence != 1,
                covered_from_at=now,
                covered_through_at=now,
                pagination_complete=False,
                query_fingerprint=b"\x00" * 32,
                evidence_hash=evidence_hash,
                expires_at=expires_at,
                row_version=1,
            )
            self._session.add(watermark)
            if source_sequence > 1:
                self._session.add(
                    PersistedExecutionGap(
                        id=new_uuid7(),
                        watermark_id=watermark.id,
                        from_sequence=1,
                        through_sequence=source_sequence - 1,
                        status="OPEN",
                    )
                )
        elif watermark.contiguous_through_sequence is None and source_sequence == 1:
            watermark.contiguous_through_sequence = 1
            initial_gap = await self._session.scalar(
                select(PersistedExecutionGap)
                .where(
                    PersistedExecutionGap.watermark_id == watermark.id,
                    PersistedExecutionGap.status == "OPEN",
                    PersistedExecutionGap.from_sequence == 1,
                )
                .with_for_update()
            )
            if initial_gap is not None:
                if initial_gap.through_sequence == 1:
                    initial_gap.status = "CLOSED"
                else:
                    initial_gap.from_sequence = 2
            sequences = list(
                (
                    await self._session.scalars(
                        select(PersistedFill.source_sequence)
                        .where(
                            PersistedFill.broker_id == broker_id,
                            PersistedFill.account_id == account_id,
                            PersistedFill.source_partition == source_partition,
                            PersistedFill.source_sequence
                            > watermark.contiguous_through_sequence,
                        )
                        .order_by(PersistedFill.source_sequence)
                        .with_for_update()
                    )
                ).all()
            )
            for sequence in sequences:
                if sequence != watermark.contiguous_through_sequence + 1:
                    break
                watermark.contiguous_through_sequence = sequence
            watermark.has_gap = (
                await self._session.scalar(
                    select(PersistedExecutionGap.id)
                    .where(
                        PersistedExecutionGap.watermark_id == watermark.id,
                        PersistedExecutionGap.status == "OPEN",
                    )
                    .limit(1)
                    .with_for_update()
                )
            ) is not None
            watermark.evidence_hash = evidence_hash
            watermark.expires_at = expires_at
            watermark.row_version += 1
        elif (
            watermark.contiguous_through_sequence is not None
            and source_sequence == watermark.contiguous_through_sequence + 1
        ):
            watermark.contiguous_through_sequence = source_sequence
            gaps = list(
                (
                    await self._session.scalars(
                        select(PersistedExecutionGap)
                        .where(
                            PersistedExecutionGap.watermark_id == watermark.id,
                            PersistedExecutionGap.status == "OPEN",
                            PersistedExecutionGap.from_sequence <= source_sequence,
                            PersistedExecutionGap.through_sequence >= source_sequence,
                        )
                        .with_for_update()
                    )
                ).all()
            )
            for gap in gaps:
                if gap.through_sequence == source_sequence:
                    gap.status = "CLOSED"
                elif gap.from_sequence == source_sequence:
                    gap.from_sequence = source_sequence + 1
            sequences = list(
                (
                    await self._session.scalars(
                        select(PersistedFill.source_sequence)
                        .where(
                            PersistedFill.broker_id == broker_id,
                            PersistedFill.account_id == account_id,
                            PersistedFill.source_partition == source_partition,
                            PersistedFill.source_sequence.is_not(None),
                            PersistedFill.source_sequence
                            > watermark.contiguous_through_sequence,
                        )
                        .order_by(PersistedFill.source_sequence)
                        .with_for_update()
                    )
                ).all()
            )
            for sequence in sequences:
                if sequence != watermark.contiguous_through_sequence + 1:
                    break
                watermark.contiguous_through_sequence = sequence
            open_gap = await self._session.scalar(
                select(PersistedExecutionGap.id)
                .where(
                    PersistedExecutionGap.watermark_id == watermark.id,
                    PersistedExecutionGap.status == "OPEN",
                )
                .limit(1)
                .with_for_update()
            )
            watermark.has_gap = open_gap is not None
            watermark.covered_through_at = now
            watermark.evidence_hash = evidence_hash
            watermark.expires_at = expires_at
            watermark.row_version += 1
        elif watermark.contiguous_through_sequence != source_sequence:
            watermark.has_gap = True
            watermark.evidence_hash = evidence_hash
            watermark.expires_at = expires_at
            watermark.row_version += 1
            gap_from = (watermark.contiguous_through_sequence or 0) + 1
            gap_through = source_sequence - 1
            if gap_through >= gap_from:
                gap = await self._session.scalar(
                    select(PersistedExecutionGap).where(
                        PersistedExecutionGap.watermark_id == watermark.id,
                        PersistedExecutionGap.from_sequence == gap_from,
                        PersistedExecutionGap.through_sequence == gap_through,
                    )
                )
                if gap is None:
                    self._session.add(
                        PersistedExecutionGap(
                            id=new_uuid7(),
                            watermark_id=watermark.id,
                            from_sequence=gap_from,
                            through_sequence=gap_through,
                            status="OPEN",
                        )
                    )
        await self._session.flush()
        return watermark

    async def terminal_completeness_proof(
        self,
        *,
        broker_id: UUID,
        account_id: UUID,
        source_partition: str,
    ) -> ExecutionCompletenessProof | None:
        watermark = await self._session.scalar(
            select(PersistedExecutionWatermark)
            .where(
                PersistedExecutionWatermark.broker_id == broker_id,
                PersistedExecutionWatermark.account_id == account_id,
                PersistedExecutionWatermark.source_partition == source_partition,
            )
            .with_for_update()
        )
        if watermark is None:
            return None
        scopes = list(
            (
                await self._session.scalars(
                    select(PersistedExecutionCheckpointScope)
                    .where(
                        PersistedExecutionCheckpointScope.watermark_id == watermark.id
                    )
                    .with_for_update()
                )
            ).all()
        )
        return ExecutionCompletenessProof(
            broker_order_ids=frozenset(
                scope.scope_value
                for scope in scopes
                if scope.scope_kind == "BROKER_ORDER_ID"
            ),
            broker_client_order_ids=frozenset(
                scope.scope_value
                for scope in scopes
                if scope.scope_kind == "BROKER_CLIENT_ORDER_ID"
            ),
            covered_from_at=watermark.covered_from_at,
            covered_through_at=watermark.covered_through_at,
            pagination_complete=watermark.pagination_complete,
            has_gap=watermark.has_gap,
            expires_at=watermark.expires_at,
        )

    async def record_closed_interval_checkpoint(
        self,
        *,
        broker_id: UUID,
        account_id: UUID,
        source_partition: str,
        broker_order_ids: tuple[str, ...],
        broker_client_order_ids: tuple[str, ...],
        covered_from_at: datetime,
        covered_through_at: datetime,
        pagination_complete: bool,
        has_gap: bool,
        expires_at: datetime,
        query_fingerprint: bytes,
        evidence_hash: bytes,
    ) -> PersistedExecutionWatermark:
        if covered_from_at > covered_through_at or expires_at <= covered_through_at:
            raise ValueError("checkpoint interval and expiry are invalid")
        if len(query_fingerprint) != 32 or len(evidence_hash) != 32:
            raise ValueError("checkpoint hashes must be SHA-256 values")
        if pagination_complete and not (broker_order_ids or broker_client_order_ids):
            raise ValueError(
                "complete checkpoint requires exact broker or client scope"
            )
        accepted = False
        watermark = await self._session.scalar(
            select(PersistedExecutionWatermark)
            .where(
                PersistedExecutionWatermark.broker_id == broker_id,
                PersistedExecutionWatermark.account_id == account_id,
                PersistedExecutionWatermark.source_partition == source_partition,
            )
            .with_for_update()
        )
        if watermark is None:
            watermark = PersistedExecutionWatermark(
                id=new_uuid7(),
                broker_id=broker_id,
                account_id=account_id,
                source_partition=source_partition,
                contiguous_through_sequence=None,
                has_gap=has_gap,
                covered_from_at=covered_from_at,
                covered_through_at=covered_through_at,
                pagination_complete=pagination_complete,
                query_fingerprint=query_fingerprint,
                evidence_hash=evidence_hash,
                expires_at=expires_at,
                row_version=1,
            )
            self._session.add(watermark)
            accepted = True
        elif (
            watermark.covered_through_at is None
            or covered_through_at >= watermark.covered_through_at
        ):
            open_gap = await self._session.scalar(
                select(PersistedExecutionGap.id)
                .where(
                    PersistedExecutionGap.watermark_id == watermark.id,
                    PersistedExecutionGap.status == "OPEN",
                )
                .limit(1)
                .with_for_update()
            )
            watermark.has_gap = has_gap or open_gap is not None
            watermark.covered_from_at = covered_from_at
            watermark.covered_through_at = covered_through_at
            watermark.pagination_complete = pagination_complete
            watermark.query_fingerprint = query_fingerprint
            watermark.evidence_hash = evidence_hash
            watermark.expires_at = expires_at
            watermark.row_version += 1
            accepted = True
        if accepted:
            await self._session.execute(
                delete(PersistedExecutionCheckpointScope).where(
                    PersistedExecutionCheckpointScope.watermark_id == watermark.id
                )
            )
            for scope_kind, values in (
                ("BROKER_ORDER_ID", broker_order_ids),
                ("BROKER_CLIENT_ORDER_ID", broker_client_order_ids),
            ):
                for scope_value in values:
                    await self._session.execute(
                        insert(PersistedExecutionCheckpointScope)
                        .values(
                            id=new_uuid7(),
                            watermark_id=watermark.id,
                            scope_kind=scope_kind,
                            scope_value=scope_value,
                        )
                        .prefix_with("IGNORE")
                    )
        await self._session.flush()
        return watermark
