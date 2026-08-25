from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from autotrader.integrations.brokers.common import BrokerResponse
from autotrader.integrations.brokers.toss.us_orders import (
    ProviderTimeWindow,
    TossUsOrderCapture,
    TossUsOrderPage,
    TossUsOrdersUnavailable,
    read_toss_us_orders,
)

SCOPE = hashlib.sha256(b"disabled-toss-us-binding").digest()
START = datetime(2026, 8, 24, 13, 0, tzinfo=UTC)
END = START + timedelta(hours=1)
WINDOW = ProviderTimeWindow(started_at=START, ended_at=END)


def _order(
    order_id: str,
    *,
    symbol: str = "AAPL",
    currency: str = "USD",
    status: str = "PENDING",
    quantity: str = "2",
    filled: str = "0",
    price: str | None = "180",
    commission: str | None = None,
    tax: str | None = None,
    ordered_at: str = "2026-08-24T22:10:00+09:00",
    filled_at: str | None = None,
) -> dict[str, object]:
    return {
        "orderId": order_id,
        "symbol": symbol,
        "side": "BUY",
        "orderType": "LIMIT" if price is not None else "MARKET",
        "timeInForce": "DAY",
        "status": status,
        "price": price,
        "quantity": quantity,
        "orderAmount": None,
        "currency": currency,
        "orderedAt": ordered_at,
        "canceledAt": None,
        "execution": {
            "filledQuantity": filled,
            "averageFilledPrice": price if filled != "0" else None,
            "filledAmount": "180" if filled != "0" else None,
            "commission": commission,
            "tax": tax,
            "filledAt": filled_at,
            "settlementDate": "2026-08-26" if filled_at is not None else None,
        },
    }


def _page(
    *orders: dict[str, object],
    next_cursor: str | None = None,
    has_next: bool = False,
    requested_cursor: str | None = None,
) -> TossUsOrderPage:
    return TossUsOrderPage(
        requested_cursor=requested_cursor,
        response=BrokerResponse(
            status=200,
            body=json.dumps(
                {
                    "result": {
                        "orders": list(orders),
                        "nextCursor": next_cursor,
                        "hasNext": has_next,
                    }
                },
                separators=(",", ":"),
            ).encode(),
        ),
    )


@pytest.mark.asyncio
async def test_reads_open_closed_partial_fills_fees_and_pagination() -> None:
    captures = (
        TossUsOrderCapture(
            status="OPEN",
            account_scope_digest=SCOPE,
            pages=(_page(_order("open-1")),),
        ),
        TossUsOrderCapture(
            status="CLOSED",
            account_scope_digest=SCOPE,
            pages=(
                _page(
                    _order(
                        "partial-1",
                        status="PARTIAL_FILLED",
                        filled="1",
                        commission="0.15",
                        tax="0",
                        filled_at="2026-08-24T22:20:00+09:00",
                    ),
                    next_cursor="cursor-2",
                    has_next=True,
                ),
                _page(
                    _order(
                        "filled-1",
                        status="FILLED",
                        quantity="1",
                        filled="1",
                        commission="0.20",
                        tax=None,
                        filled_at="2026-08-24T22:30:00+09:00",
                    ),
                    requested_cursor="cursor-2",
                ),
            ),
        ),
    )

    facts = await read_toss_us_orders(
        WINDOW,
        captures=captures,
        captured_at=END,
    )

    assert [fact.provider_order_id for fact in facts] == [
        "filled-1",
        "open-1",
        "partial-1",
    ]
    open_fact = facts[1]
    assert open_fact.commission is None
    assert open_fact.tax is None
    assert open_fact.ordered_at == datetime(2026, 8, 24, 13, 10, tzinfo=UTC)
    partial = facts[2]
    assert partial.cumulative_fill_quantity == Decimal("1")
    assert partial.commission == Decimal("0.15")
    assert partial.tax == Decimal("0")
    assert len(partial.source_digest) == 32


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "captures",
    [
        (
            TossUsOrderCapture(
                status="OPEN",
                account_scope_digest=None,
                pages=(_page(),),
            ),
            TossUsOrderCapture(
                status="CLOSED",
                account_scope_digest=SCOPE,
                pages=(_page(),),
            ),
        ),
        (
            TossUsOrderCapture(
                status="OPEN",
                account_scope_digest=SCOPE,
                pages=(_page(_order("same")),),
            ),
            TossUsOrderCapture(
                status="CLOSED",
                account_scope_digest=SCOPE,
                pages=(_page(_order("same")),),
            ),
        ),
        (
            TossUsOrderCapture(
                status="OPEN",
                account_scope_digest=SCOPE,
                pages=(_page(),),
            ),
            TossUsOrderCapture(
                status="CLOSED",
                account_scope_digest=SCOPE,
                pages=(_page(next_cursor="missing", has_next=True),),
            ),
        ),
    ],
)
async def test_scope_duplicates_or_missing_page_never_pass(
    captures: tuple[TossUsOrderCapture, TossUsOrderCapture],
) -> None:
    with pytest.raises(TossUsOrdersUnavailable, match="incomplete"):
        await read_toss_us_orders(WINDOW, captures=captures, captured_at=END)


@pytest.mark.asyncio
async def test_ignores_valid_kr_order_but_rejects_bad_provider_timestamp() -> None:
    valid_mixed = (
        TossUsOrderCapture(
            status="OPEN",
            account_scope_digest=SCOPE,
            pages=(_page(_order("kr-1", symbol="005930", currency="KRW")),),
        ),
        TossUsOrderCapture(
            status="CLOSED",
            account_scope_digest=SCOPE,
            pages=(_page(),),
        ),
    )
    invalid_time = (
        TossUsOrderCapture(
            status="OPEN",
            account_scope_digest=SCOPE,
            pages=(_page(_order("bad-time", ordered_at="2026-08-24T13:10:00")),),
        ),
        TossUsOrderCapture(
            status="CLOSED",
            account_scope_digest=SCOPE,
            pages=(_page(),),
        ),
    )

    assert (
        await read_toss_us_orders(WINDOW, captures=valid_mixed, captured_at=END) == ()
    )
    with pytest.raises(TossUsOrdersUnavailable, match="incomplete"):
        await read_toss_us_orders(WINDOW, captures=invalid_time, captured_at=END)
