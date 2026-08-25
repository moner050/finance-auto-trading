from __future__ import annotations

from dataclasses import replace
from typing import ClassVar

from autotrader.execution.orders.models import (
    BrokerOrderLinkState,
    BrokerOrderStatusEvent,
    BrokerStatusWatermark,
    DeferredBrokerStatus,
    Order,
    OrderStatus,
    OrderTransition,
)


class OrderStateMachine:
    _STATUSES: ClassVar[dict[str, OrderStatus]] = {
        status.value: status for status in OrderStatus
    }
    _ALLOWED: ClassVar[dict[OrderStatus, frozenset[OrderStatus]]] = {
        OrderStatus.CREATED: frozenset(
            {OrderStatus.SUBMITTED, OrderStatus.EXPIRED, OrderStatus.UNKNOWN}
        ),
        OrderStatus.SUBMITTED: frozenset(
            {
                OrderStatus.ACKNOWLEDGED,
                OrderStatus.REJECTED,
                OrderStatus.CANCEL_PENDING,
                OrderStatus.CANCELED,
                OrderStatus.EXPIRED,
                OrderStatus.UNKNOWN,
            }
        ),
        OrderStatus.ACKNOWLEDGED: frozenset(
            {
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
                OrderStatus.CANCEL_PENDING,
                OrderStatus.CANCELED,
                OrderStatus.UNKNOWN,
            }
        ),
        OrderStatus.PARTIALLY_FILLED: frozenset(
            {
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
                OrderStatus.CANCEL_PENDING,
                OrderStatus.CANCELED,
                OrderStatus.UNKNOWN,
            }
        ),
        OrderStatus.CANCEL_PENDING: frozenset(
            {
                OrderStatus.CANCELED,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
                OrderStatus.UNKNOWN,
            }
        ),
        OrderStatus.UNKNOWN: frozenset({OrderStatus.UNKNOWN}),
        OrderStatus.FILLED: frozenset(),
        OrderStatus.CANCELED: frozenset(),
        OrderStatus.REJECTED: frozenset(),
        OrderStatus.EXPIRED: frozenset(),
    }

    def apply(
        self,
        order: Order,
        event: BrokerOrderStatusEvent,
        *,
        links: tuple[BrokerOrderLinkState, ...] | None = None,
        watermarks: tuple[BrokerStatusWatermark, ...] = (),
    ) -> OrderTransition | DeferredBrokerStatus:
        if event.account_id != order.account_id:
            raise InvalidOrderTransitionError(
                "broker event account does not match order"
            )
        watermark = next(
            (
                item
                for item in watermarks
                if item.source_partition == event.source_partition
            ),
            None,
        )
        if (
            event.source_sequence is not None
            and watermark is not None
            and (event.source_sequence <= watermark.last_contiguous_sequence)
        ):
            return DeferredBrokerStatus(
                order=order,
                event=event,
                reason="STALE_SEQUENCE",
            )
        expected_sequence = (
            watermark.last_contiguous_sequence + 1 if watermark is not None else 1
        )
        if (
            event.source_sequence is not None
            and event.source_sequence > expected_sequence
        ):
            return DeferredBrokerStatus(
                order=order,
                event=event,
                reason="SEQUENCE_GAP",
                missing_from_sequence=expected_sequence,
            )
        target = self._STATUSES.get(event.raw_status, OrderStatus.UNKNOWN)
        if target in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        } and not self._can_terminalize(event=event, links=links):
            return DeferredBrokerStatus(
                order=order,
                event=event,
                reason="LIVE_SUCCESSOR_LINK",
                missing_from_sequence=expected_sequence,
            )
        if target not in self._ALLOWED[order.status]:
            if (
                order.status is OrderStatus.PARTIALLY_FILLED
                and target is OrderStatus.ACKNOWLEDGED
                and event.source_sequence is not None
            ):
                return DeferredBrokerStatus(
                    order=order,
                    event=event,
                    reason="STALE_STATUS",
                )
            raise InvalidOrderTransitionError(
                f"{order.status.value} cannot transition to {target.value}"
            )
        transition = OrderTransition.create(
            order=order,
            status=target,
            raw_status=event.raw_status,
            occurred_at=event.occurred_at,
        )
        if event.source_sequence is None:
            return transition
        return replace(
            transition,
            watermark=BrokerStatusWatermark(
                source_partition=event.source_partition,
                last_contiguous_sequence=event.source_sequence,
            ),
        )

    @staticmethod
    def _can_terminalize(
        *,
        event: BrokerOrderStatusEvent,
        links: tuple[BrokerOrderLinkState, ...] | None,
    ) -> bool:
        if not links:
            return False
        if event.broker_order_id is None:
            return False
        matching_link = next(
            (link for link in links if link.broker_order_id == event.broker_order_id),
            None,
        )
        if matching_link is None:
            return False
        updated_links = tuple(
            replace(link, status=OrderStatus(event.raw_status))
            if link.id == matching_link.id
            else link
            for link in links
        )
        return all(link.is_terminal for link in updated_links if link.exposure_bearing)


class InvalidOrderTransitionError(ValueError):
    pass
