from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
ARTIFACT = ROOT / "docs" / "providers" / "toss" / "us-cash-v6.json"
CAPTURED_AT = datetime(2026, 8, 24, 13, 51, 33, tzinfo=UTC)


def _load(*, now: datetime) -> dict[str, object]:
    evidence = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    captured_at = datetime.fromisoformat(
        str(evidence["captured_at"]).replace("Z", "+00:00")
    )
    maximum_age = timedelta(days=int(evidence["maximum_age_days"]))
    if now - captured_at > maximum_age:
        raise ValueError("Toss US cash evidence is stale")

    expected = evidence.pop("artifact_sha256")
    canonical = json.dumps(
        evidence,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == expected
    return evidence


def test_toss_us_cash_contract_is_current_exact_and_sanitized() -> None:
    evidence = _load(now=CAPTURED_AT + timedelta(days=1))

    assert evidence["kind"] == "TOSS_US_CASH_V6_PROVIDER_CONTRACT"
    assert evidence["provider"] == "TOSS"
    assert evidence["captured_at"] == "2026-08-24T13:51:33Z"
    assert evidence["scope"] == {
        "market_country": "US",
        "instrument_type": "COMMON_STOCK",
        "settlement_asset": "USD",
        "position_mode": "LONG_ONLY",
        "submission_mode": "QUANTITY_ONLY",
    }

    sources = evidence["sources"]
    assert sources["openapi"] == {
        "url": "https://openapi.tossinvest.com/openapi-docs/latest/openapi.json",
        "hash_of": "RAW_RESPONSE_BYTES",
        "openapi": "3.1.0",
        "version": "1.2.14",
        "bytes": 417769,
        "sha256": "a7b32ba754401d13fa649ba91eebd212420eb1afab28e9c2c0d6ea8d43055fed",
    }
    assert sources["overview"] == {
        "url": "https://openapi.tossinvest.com/openapi-docs/overview.md",
        "hash_of": "RAW_RESPONSE_BYTES",
        "bytes": 27864,
        "sha256": "dfad8c9251917daf39d2b2a9e455f0d7cadddafb42a34f47b2ee8d67bf4addd8",
    }

    fields = evidence["fields"]
    assert fields["quantity_order_create_required"] == [
        "symbol",
        "side",
        "orderType",
        "quantity",
    ]
    assert fields["quantity_order_create_optional"] == [
        "clientOrderId",
        "timeInForce",
        "price",
        "confirmHighValueOrder",
    ]
    assert fields["order_required"] == [
        "orderId",
        "symbol",
        "side",
        "orderType",
        "timeInForce",
        "status",
        "quantity",
        "currency",
        "orderedAt",
        "execution",
    ]
    assert fields["execution_required"] == [
        "filledQuantity",
        "averageFilledPrice",
        "filledAmount",
        "commission",
        "tax",
        "filledAt",
        "settlementDate",
    ]
    assert fields["buying_power_required"] == ["currency", "cashBuyingPower"]
    assert fields["sellable_quantity_required"] == ["sellableQuantity"]
    assert fields["holdings_overview_required"] == [
        "totalPurchaseAmount",
        "marketValue",
        "profitLoss",
        "dailyProfitLoss",
        "items",
    ]
    assert fields["holding_item_required"] == [
        "symbol",
        "name",
        "marketCountry",
        "currency",
        "quantity",
        "lastPrice",
        "averagePurchasePrice",
        "marketValue",
        "profitLoss",
        "dailyProfitLoss",
        "cost",
    ]
    assert fields["commission_required"] == ["marketCountry", "commissionRate"]
    assert fields["paginated_order_required"] == ["orders", "nextCursor", "hasNext"]

    facts = evidence["provider_facts"]
    assert facts["usd_buying_power"] == {
        "state": "AVAILABLE",
        "source_field": "BuyingPowerResponse.cashBuyingPower",
    }
    assert facts["fills_and_fees"] == {
        "state": "AVAILABLE",
        "source_fields": [
            "Order.execution.filledQuantity",
            "Order.execution.averageFilledPrice",
            "Order.execution.filledAmount",
            "Order.execution.commission",
            "Order.execution.tax",
        ],
    }
    assert facts["holdings"]["state"] == "AVAILABLE"
    assert facts["sellable_quantity"]["state"] == "AVAILABLE"
    assert facts["commission_schedule"]["state"] == "AVAILABLE"
    assert facts["client_order_id_in_order_list_or_detail"]["state"] == "UNAVAILABLE"
    assert facts["provider_account_identity_in_order_list_or_detail"]["state"] == (
        "UNAVAILABLE"
    )

    recovery = evidence["submission_recovery"]
    assert recovery["idempotency_field"] == "clientOrderId"
    assert recovery["window_seconds"] == 600
    assert recovery["maximum_replays"] == 1
    assert recovery["list_or_detail_recovery_allowed"] is False

    assert evidence["rate_limits"] == {
        "AUTH": {"normal_tps": 5},
        "ACCOUNT": {"normal_tps": 1},
        "ASSET": {"normal_tps": 5},
        "ORDER": {
            "normal_tps": 10,
            "peak_kst": "09:00:00/09:10:00",
            "peak_tps": 10,
        },
        "ORDER_HISTORY": {"normal_tps": 5},
        "ORDER_INFO": {
            "normal_tps": 6,
            "peak_kst": "09:00:00/09:10:00",
            "peak_tps": 3,
        },
    }

    lowered = ARTIFACT.read_bytes().lower()
    for forbidden in (b"authorization", b"access_token", b"account_seq"):
        assert forbidden not in lowered


def test_toss_us_cash_contract_fails_closed_when_stale() -> None:
    with pytest.raises(ValueError, match="stale"):
        _load(now=CAPTURED_AT + timedelta(days=30, seconds=1))
