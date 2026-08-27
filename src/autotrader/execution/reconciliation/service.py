from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from autotrader.execution.reconciliation.models import (
    BrokerSnapshot,
    HeldPosition,
    InternalOpenOrder,
    ReconciliationDiff,
    ReconciliationDiffKind,
    ReconciliationRun,
)
from autotrader.shared.ids import new_uuid7


class BrokerSnapshotReader(Protocol):
    async def read_snapshot(
        self, *, account_id: object, now: datetime
    ) -> BrokerSnapshot:
        """What the broker says, and the moment the answer is about.

        Freshness is a property of an instant, and the snapshot carries an
        expiry that the comparison checks, so the reader cannot be left to
        pick its own idea of now.
        """
        ...


class ReconciliationRunStore(Protocol):
    async def persist_run(self, run: ReconciliationRun) -> ReconciliationRun: ...


class ReconciliationService:
    """Compares evidence without changing internal orders or positions."""

    def compare(
        self,
        *,
        now: datetime,
        snapshot: BrokerSnapshot,
        internal_open_orders: tuple[InternalOpenOrder, ...],
        internal_positions: tuple[HeldPosition, ...],
    ) -> tuple[ReconciliationDiff, ...]:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        diffs: list[ReconciliationDiff] = []
        if not snapshot.complete:
            diffs.append(
                ReconciliationDiff(
                    kind=ReconciliationDiffKind.SNAPSHOT_INCOMPLETE,
                    blocking=True,
                    internal_order_id=None,
                    broker_order_id=None,
                )
            )
        if snapshot.expires_at <= now:
            diffs.append(
                ReconciliationDiff(
                    kind=ReconciliationDiffKind.SNAPSHOT_STALE,
                    blocking=True,
                    internal_order_id=None,
                    broker_order_id=None,
                )
            )
        broker_by_identity = {
            (order.broker_order_id, order.broker_client_order_id): order
            for order in snapshot.open_orders
        }
        internal_by_identity = {
            (order.broker_order_id, order.broker_client_order_id): order
            for order in internal_open_orders
        }
        for identity, internal in internal_by_identity.items():
            if identity not in broker_by_identity:
                diffs.append(
                    ReconciliationDiff(
                        kind=ReconciliationDiffKind.INTERNAL_OPEN_BROKER_MISSING,
                        blocking=True,
                        internal_order_id=internal.order_id,
                        broker_order_id=internal.broker_order_id,
                    )
                )
        for identity, broker in broker_by_identity.items():
            if identity not in internal_by_identity:
                diffs.append(
                    ReconciliationDiff(
                        kind=ReconciliationDiffKind.BROKER_OPEN_INTERNAL_MISSING,
                        blocking=True,
                        internal_order_id=None,
                        broker_order_id=broker.broker_order_id,
                    )
                )
        diffs.extend(_position_diffs(snapshot.positions, internal_positions))
        return tuple(diffs)

    async def run(
        self,
        *,
        now: datetime,
        account_id: object,
        reader: BrokerSnapshotReader,
        store: ReconciliationRunStore,
        internal_open_orders: tuple[InternalOpenOrder, ...],
        internal_positions: tuple[HeldPosition, ...],
    ) -> ReconciliationRun:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        snapshot = await reader.read_snapshot(account_id=account_id, now=now)
        if snapshot.account_id != account_id:
            raise ValueError("broker snapshot account does not match requested account")
        diffs = self.compare(
            now=now,
            snapshot=snapshot,
            internal_open_orders=internal_open_orders,
            internal_positions=internal_positions,
        )
        run = ReconciliationRun(
            id=new_uuid7(),
            broker_id=snapshot.broker_id,
            account_id=snapshot.account_id,
            snapshot_hash=_run_hash(
                snapshot, internal_open_orders, internal_positions, diffs
            ),
            complete=snapshot.complete,
            succeeded=snapshot.complete,
            diffs=diffs,
            started_at=now,
            completed_at=now,
        )
        return await store.persist_run(run)


def _position_diffs(
    broker: tuple[HeldPosition, ...],
    internal: tuple[HeldPosition, ...],
) -> list[ReconciliationDiff]:
    """Every way the two sides can disagree about what is held.

    All of them block. A position the broker does not report, one it reports
    that we do not, and a quantity that does not match are the same problem
    wearing three faces: the account is not what this system believes, and
    every size it calculates from here is wrong.
    """
    broker_by_instrument = {held.instrument_id: held for held in broker}
    internal_by_instrument = {held.instrument_id: held for held in internal}
    diffs: list[ReconciliationDiff] = []
    for instrument_id in sorted(
        set(broker_by_instrument) | set(internal_by_instrument), key=str
    ):
        theirs = broker_by_instrument.get(instrument_id)
        ours = internal_by_instrument.get(instrument_id)
        if theirs is None:
            kind = ReconciliationDiffKind.INTERNAL_POSITION_BROKER_MISSING
        elif ours is None:
            kind = ReconciliationDiffKind.BROKER_POSITION_INTERNAL_MISSING
        elif theirs.quantity != ours.quantity:
            kind = ReconciliationDiffKind.POSITION_QUANTITY_MISMATCH
        else:
            continue
        diffs.append(
            ReconciliationDiff(
                kind=kind,
                blocking=True,
                internal_order_id=None,
                broker_order_id=None,
                instrument_id=instrument_id,
            )
        )
    return diffs


def _held_payload(positions: tuple[HeldPosition, ...]) -> list[dict[str, str]]:
    return [
        {
            "instrument_id": str(held.instrument_id),
            "quantity": format(held.quantity, "f"),
        }
        for held in sorted(positions, key=lambda held: str(held.instrument_id))
    ]


def _run_hash(
    snapshot: BrokerSnapshot,
    internal_open_orders: tuple[InternalOpenOrder, ...],
    internal_positions: tuple[HeldPosition, ...],
    diffs: tuple[ReconciliationDiff, ...],
) -> bytes:
    payload = {
        "account_id": str(snapshot.account_id),
        "broker_id": str(snapshot.broker_id),
        "complete": snapshot.complete,
        "expires_at": snapshot.expires_at.isoformat(),
        "open_orders": [
            {
                "broker_client_order_id": order.broker_client_order_id,
                "broker_order_id": order.broker_order_id,
                "canonical_terms_hash": order.canonical_terms_hash.hex(),
            }
            for order in sorted(
                snapshot.open_orders,
                key=lambda order: (
                    order.broker_order_id,
                    order.broker_client_order_id,
                ),
            )
        ],
        "internal_open_orders": [
            {
                "broker_client_order_id": order.broker_client_order_id,
                "broker_order_id": order.broker_order_id,
                "order_id": str(order.order_id),
            }
            for order in sorted(
                internal_open_orders,
                key=lambda order: (
                    order.broker_order_id,
                    order.broker_client_order_id,
                    str(order.order_id),
                ),
            )
        ],
        "broker_positions": _held_payload(snapshot.positions),
        "internal_positions": _held_payload(internal_positions),
        "diffs": [
            {
                "blocking": diff.blocking,
                "broker_order_id": diff.broker_order_id,
                "instrument_id": str(diff.instrument_id)
                if diff.instrument_id
                else None,
                "internal_order_id": str(diff.internal_order_id)
                if diff.internal_order_id
                else None,
                "kind": diff.kind.value,
            }
            for diff in sorted(
                diffs,
                key=lambda diff: (
                    diff.kind.value,
                    str(diff.internal_order_id or ""),
                    diff.broker_order_id or "",
                    str(diff.instrument_id or ""),
                ),
            )
        ],
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).digest()
