from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from autotrader.integrations.brokers.common import BrokerResponse
from autotrader.integrations.brokers.toss.us_account_snapshot import (
    TossUsSnapshotCapture,
    TossUsSnapshotUnavailable,
    capture_toss_us_snapshot,
)

AS_OF = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)
SCOPE = hashlib.sha256(b"disabled-toss-us-binding").digest()


def _response(result: object) -> BrokerResponse:
    return BrokerResponse(
        status=200,
        body=json.dumps({"result": result}, separators=(",", ":")).encode(),
    )


def _holding(
    *,
    symbol: str,
    market_country: str,
    currency: str,
    quantity: str,
    average_price: str,
    market_value: str,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "name": f"name-{symbol}",
        "marketCountry": market_country,
        "currency": currency,
        "quantity": quantity,
        "lastPrice": market_value,
        "averagePurchasePrice": average_price,
        "marketValue": {
            "purchaseAmount": average_price,
            "amount": market_value,
            "amountAfterCost": market_value,
        },
        "profitLoss": {
            "amount": "0",
            "amountAfterCost": "0",
            "rate": "0",
            "rateAfterCost": "0",
        },
        "dailyProfitLoss": {"amount": "0", "rate": "0"},
        "cost": {"commission": "0", "tax": None},
    }


def _holdings(*items: dict[str, object]) -> BrokerResponse:
    return _response(
        {
            "totalPurchaseAmount": {"krw": "0", "usd": "150"},
            "marketValue": {
                "amount": {"krw": "0", "usd": "180"},
                "amountAfterCost": {"krw": "0", "usd": "179.9"},
            },
            "profitLoss": {
                "amount": {"krw": "0", "usd": "30"},
                "amountAfterCost": {"krw": "0", "usd": "29.9"},
                "rate": "0.2",
                "rateAfterCost": "0.1993",
            },
            "dailyProfitLoss": {
                "amount": {"krw": "0", "usd": "1"},
                "rate": "0.005",
            },
            "items": list(items),
        }
    )


def _capture(*, scope: bytes | None = SCOPE) -> TossUsSnapshotCapture:
    return TossUsSnapshotCapture(
        account_scope_digest=scope,
        buying_power_response=_response(
            {"currency": "USD", "cashBuyingPower": "3500.50"}
        ),
        holdings_response=_holdings(
            _holding(
                symbol="005930",
                market_country="KR",
                currency="KRW",
                quantity="10",
                average_price="70000",
                market_value="710000",
            ),
            _holding(
                symbol="AAPL",
                market_country="US",
                currency="USD",
                quantity="1.5",
                average_price="100",
                market_value="180",
            ),
        ),
        sellable_responses={"AAPL": _response({"sellableQuantity": "1.25"})},
    )


@pytest.mark.asyncio
async def test_captures_direct_usd_cash_and_us_sellable_positions() -> None:
    snapshot = await capture_toss_us_snapshot(
        AS_OF,
        capture=_capture(),
        captured_at=AS_OF + timedelta(seconds=1),
    )

    assert snapshot.account_scope_digest == SCOPE
    assert snapshot.cash_fact.state == "AVAILABLE"
    assert snapshot.cash_fact.available_cash == Decimal("3500.50")
    assert snapshot.cash_fact.settled_cash is None
    assert snapshot.cash_fact.source_field == "cashBuyingPower"
    assert snapshot.positions[0].symbol == "AAPL"
    assert snapshot.positions[0].total_quantity == Decimal("1.5")
    assert snapshot.positions[0].sellable_quantity == Decimal("1.25")
    assert snapshot.positions[0].average_price == Decimal("100")
    assert snapshot.positions[0].market_value == Decimal("180")
    assert snapshot.holdings_page_count == 1
    assert snapshot.sellable_page_count == 1
    assert len(snapshot.source_digest) == 32
    assert repr(SCOPE) not in repr(snapshot)
    assert "disabled-toss-us-binding" not in repr(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capture",
    [
        TossUsSnapshotCapture(
            account_scope_digest=SCOPE,
            buying_power_response=_response(
                {"currency": "KRW", "cashBuyingPower": "1000"}
            ),
            holdings_response=_holdings(),
            sellable_responses={},
        ),
        TossUsSnapshotCapture(
            account_scope_digest=SCOPE,
            buying_power_response=_response({"currency": "USD"}),
            holdings_response=_holdings(),
            sellable_responses={},
        ),
        _capture(scope=None),
    ],
)
async def test_missing_direct_usd_or_account_scope_never_passes(
    capture: TossUsSnapshotCapture,
) -> None:
    with pytest.raises(TossUsSnapshotUnavailable, match="incomplete"):
        await capture_toss_us_snapshot(AS_OF, capture=capture, captured_at=AS_OF)


@pytest.mark.asyncio
async def test_missing_sellable_or_duplicate_us_holding_never_passes() -> None:
    missing = _capture()
    missing = TossUsSnapshotCapture(
        account_scope_digest=missing.account_scope_digest,
        buying_power_response=missing.buying_power_response,
        holdings_response=missing.holdings_response,
        sellable_responses={},
    )
    duplicate = _holding(
        symbol="AAPL",
        market_country="US",
        currency="USD",
        quantity="1",
        average_price="101",
        market_value="180",
    )
    duplicated = TossUsSnapshotCapture(
        account_scope_digest=SCOPE,
        buying_power_response=_response({"currency": "USD", "cashBuyingPower": "3500"}),
        holdings_response=_holdings(duplicate, duplicate),
        sellable_responses={"AAPL": _response({"sellableQuantity": "1"})},
    )

    for capture in (missing, duplicated):
        with pytest.raises(TossUsSnapshotUnavailable, match="incomplete"):
            await capture_toss_us_snapshot(AS_OF, capture=capture, captured_at=AS_OF)
