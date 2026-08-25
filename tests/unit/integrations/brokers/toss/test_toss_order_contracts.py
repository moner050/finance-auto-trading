from __future__ import annotations

import importlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import cast
from uuid import uuid7

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.common import BrokerMarket
from autotrader.integrations.brokers.toss import adapter

NOW = datetime(2026, 8, 18, tzinfo=UTC)
EVIDENCE = (
    Path(__file__).resolve().parents[5]
    / "docs/providers/toss/openapi-1.2.14-order-contract.sanitized.json"
)


def _command(**overrides: object) -> BrokerOrderCommand:
    values: dict[str, object] = {
        "id": uuid7(),
        "order_id": uuid7(),
        "account_id": uuid7(),
        "instrument_id": uuid7(),
        "command_type": CommandType.SUBMIT,
        "target_aggregate_version": 1,
        "idempotency_key": f"submit:{uuid7()}",
        "command_sequence": 1,
        "canonical_payload_hash": b"p" * 32,
        "broker_client_order_id": uuid7().hex,
        "target_broker_order_id": None,
        "replaces_command_id": None,
        "origin_type": "STRATEGY",
        "authority_class": "SUBMIT_NEW_EXPOSURE",
        "owner_runtime_instance_id": uuid7(),
        "fencing_token": 1,
        "not_after": NOW + timedelta(minutes=1),
        "side": Side.BUY,
        "order_style": OrderStyle.LIMIT,
        "quantity": Decimal("2"),
        "limit_price": Decimal("70000"),
        "time_in_force": "DAY",
    }
    values.update(overrides)
    return BrokerOrderCommand(**values)  # type: ignore[arg-type]


def _contract_module() -> object:
    return importlib.import_module(
        "autotrader.integrations.brokers.toss.stock_order_contracts"
    )


def _evidence() -> dict[str, object]:
    return json.loads(EVIDENCE.read_text(encoding="utf-8"))


def test_adapter_keeps_exact_stock_order_contract_reexport_identities() -> None:
    contracts = _contract_module()

    for name in (
        "TossStockOrderPreviewError",
        "TossStockOrderPreview",
        "TossOrderSubmissionAcknowledgement",
        "build_toss_stock_order_preview",
        "decode_toss_order_submission_acknowledgement",
    ):
        assert getattr(adapter, name) is getattr(contracts, name)


def test_stock_order_contract_fresh_import_loads_no_operational_modules() -> None:
    code = """
import sys
import autotrader.integrations.brokers.toss.stock_order_contracts
blocked = (
    'autotrader.apps',
    'autotrader.config',
    'autotrader.execution',
    'autotrader.operations',
    'autotrader.persistence',
    'autotrader.integrations.brokers.common',
    'autotrader.integrations.brokers.toss.adapter',
    'autotrader.integrations.brokers.toss.transport',
)
loaded = sorted(name for name in sys.modules if name.startswith(blocked))
if loaded:
    raise SystemExit(';'.join(loaded))
"""

    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_sanitized_openapi_evidence_pins_submit_and_recovery_boundary() -> None:
    evidence = _evidence()

    assert evidence["source"] == {
        "url": "https://openapi.tossinvest.com/openapi-docs/latest/openapi.json",
        "capturedAt": "2026-08-18",
        "openapi": "3.1.0",
        "version": "1.2.14",
        "sha256": "d29f9079a557c0b6affcec330aa131f93b09fd49932354668e3dc4524cd42180",
    }
    submit = evidence["submit"]
    assert isinstance(submit, dict)
    assert (submit["method"], submit["path"], submit["contentType"]) == (
        "POST",
        "/api/v1/orders",
        "application/json",
    )
    assert submit["authorizationHeader"] == "Authorization"
    assert submit["authorizationScheme"] == "Bearer"
    assert submit["accountHeader"] == {
        "name": "X-Tossinvest-Account",
        "required": True,
        "source": "GET /api/v1/accounts result[].accountSeq",
        "schemaType": "integer",
        "schemaFormat": "int64",
    }
    request = cast(dict[str, object], submit["krxQuantityRequest"])
    assert request["required"] == ["symbol", "side", "orderType", "quantity"]
    assert request["properties"] == [
        "clientOrderId",
        "symbol",
        "side",
        "orderType",
        "timeInForce",
        "quantity",
        "price",
        "confirmHighValueOrder",
    ]
    assert request["clientOrderId"] == {
        "required": False,
        "maxLength": 36,
        "pattern": "^[a-zA-Z0-9\\-_]+$",
        "idempotencyWindowSeconds": 600,
    }
    assert request["confirmHighValueOrder"] == {
        "default": False,
        "repositoryFixedValue": False,
        "threshold": "100000000",
        "currency": "KRW",
        "thresholdComparison": "GREATER_THAN_OR_EQUAL",
        "failureStatus": 400,
        "failureCode": "confirm-high-value-required",
    }
    assert submit["success"] == {
        "status": 200,
        "envelopeRequired": ["result"],
        "resultRequired": ["orderId"],
        "resultProperties": ["orderId", "clientOrderId"],
        "clientOrderIdNullableWhenRequestOmitted": True,
    }
    assert submit["conflicts"] == [
        {"status": 409, "code": "request-in-progress"},
        {"status": 422, "code": "idempotency-key-conflict"},
    ]
    assert evidence["implementationBoundary"] == {
        "krxHighValuePreflight": "PRICE_KNOWN_LIMIT_ONLY",
        "krxMarketNotionalPreflightSupported": False,
        "laboratoryWriterScope": "KRX_LIMIT_DAY_ONLY",
        "autoConfirmHighValueOrder": False,
    }

    recovery = evidence["recovery"]
    assert isinstance(recovery, dict)
    assert recovery["list"] == {
        "method": "GET",
        "path": "/api/v1/orders",
        "requiredQuery": ["status"],
        "optionalQuery": ["symbol", "from", "to", "cursor", "limit"],
        "statusValues": ["OPEN", "CLOSED"],
        "open": {
            "returnsAll": True,
            "ignoresQuery": ["cursor", "limit"],
            "nextCursor": None,
            "hasNext": False,
        },
        "closed": {
            "defaultLimit": 20,
            "maximumLimit": 100,
            "cursorField": "nextCursor",
            "terminationField": "hasNext",
            "terminalHasNext": False,
        },
        "resultRequired": ["orders", "nextCursor", "hasNext"],
    }
    order_record = cast(dict[str, object], recovery["orderRecord"])
    assert order_record["required"] == [
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
    assert "clientOrderId" not in cast(list[object], order_record["properties"])
    assert order_record["clientOrderIdField"] is None
    assert order_record["providerAccountCorrelationField"] is None
    assert recovery["providerAccountScope"] == {
        "requestHeader": "X-Tossinvest-Account",
        "responseRecordField": None,
    }
    assert recovery["evidenceStatus"] == ("SUPPORTED_EXACT_REPLAY_WITHIN_600_SECONDS")
    assert recovery["exactSubmitReplay"] == {
        "supported": True,
        "commandType": "SUBMIT",
        "method": "POST",
        "path": "/api/v1/orders",
        "requiresClientOrderId": True,
        "requiresIdenticalRequestBody": True,
        "successBehavior": "RETURN_PREVIOUS_ORDER_RESULT_UNCHANGED",
        "idempotencyWindowSeconds": 600,
        "windowEndExclusive": True,
        "sameKeyAfterWindowCreatesNewOrder": True,
        "pending": {"status": 409, "code": "request-in-progress"},
        "bodyConflict": {"status": 422, "code": "idempotency-key-conflict"},
    }
    assert recovery["listDetailClientOrderLookup"] == {
        "supported": False,
        "reason": (
            "Official order list/detail Order records expose neither "
            "clientOrderId nor a provider account-correlation field."
        ),
    }


@pytest.mark.parametrize(
    ("quantity", "price"),
    (
        (Decimal("1"), Decimal("100000000")),
        (Decimal("100"), Decimal("1000000")),
    ),
)
def test_krx_preview_blocks_the_exact_high_value_confirmation_boundary(
    quantity: Decimal, price: Decimal
) -> None:
    with pytest.raises(ValueError, match="unavailable"):
        adapter.build_toss_stock_order_preview(
            command=_command(quantity=quantity, limit_price=price),
            account_seq=17,
            market=BrokerMarket.KRX_STOCK,
            symbol="005930",
            now=NOW,
        )


def test_krx_preview_keeps_confirmation_false_below_high_value_boundary() -> None:
    preview = adapter.build_toss_stock_order_preview(
        command=_command(quantity=Decimal("1"), limit_price=Decimal("99999999")),
        account_seq=17,
        market=BrokerMarket.KRX_STOCK,
        symbol="005930",
        now=NOW,
    )

    assert json.loads(preview.body)["confirmHighValueOrder"] is False


@pytest.mark.parametrize(
    ("command_overrides", "market"),
    (
        ({"command_type": "SUBMIT"}, BrokerMarket.KRX_STOCK),
        ({}, "KRX_STOCK"),
        ({"side": "BUY"}, BrokerMarket.KRX_STOCK),
        ({"order_style": "LIMIT"}, BrokerMarket.KRX_STOCK),
    ),
)
def test_preview_rejects_raw_strings_in_place_of_contract_enums(
    command_overrides: dict[str, object], market: object
) -> None:
    with pytest.raises(ValueError, match="unavailable"):
        adapter.build_toss_stock_order_preview(
            command=_command(**command_overrides),
            account_seq=17,
            market=market,
            symbol="005930",
            now=NOW,
        )


@pytest.mark.parametrize(
    ("module", "name", "member", "value", "command_field"),
    (
        (
            "autotrader.execution.orders.models",
            "CommandType",
            "SUBMIT",
            "SUBMIT",
            "command_type",
        ),
        (
            "autotrader.integrations.brokers.common",
            "BrokerMarket",
            "KRX_STOCK",
            "KRX_STOCK",
            None,
        ),
        (
            "autotrader.domain.enums",
            "Side",
            "BUY",
            "BUY",
            "side",
        ),
        (
            "autotrader.domain.enums",
            "OrderStyle",
            "LIMIT",
            "LIMIT",
            "order_style",
        ),
    ),
)
def test_preview_rejects_forged_enums_that_spoof_canonical_type_metadata(
    module: str,
    name: str,
    member: str,
    value: str,
    command_field: str | None,
) -> None:
    forged_type = StrEnum(name, {member: value}, module=module)
    forged_value = next(iter(forged_type))
    command_overrides = {} if command_field is None else {command_field: forged_value}
    market: object = forged_value if command_field is None else BrokerMarket.KRX_STOCK

    with pytest.raises(ValueError, match="unavailable"):
        adapter.build_toss_stock_order_preview(
            command=_command(**command_overrides),
            account_seq=17,
            market=market,
            symbol="005930",
            now=NOW,
        )


def test_preview_accepts_the_exact_canonical_enum_types() -> None:
    preview = adapter.build_toss_stock_order_preview(
        command=_command(
            command_type=CommandType.SUBMIT,
            side=Side.BUY,
            order_style=OrderStyle.LIMIT,
        ),
        account_seq=17,
        market=BrokerMarket.KRX_STOCK,
        symbol="005930",
        now=NOW,
    )

    assert json.loads(preview.body)["side"] == "BUY"


@pytest.mark.parametrize(
    ("symbol", "expected"),
    (
        ("aapl", "AAPL"),
        ("삼성", "삼성"),
        ("a" * 33, "A" * 33),
    ),
)
def test_preview_preserves_the_pre_extraction_symbol_normalization(
    symbol: str, expected: str
) -> None:
    preview = adapter.build_toss_stock_order_preview(
        command=_command(),
        account_seq=17,
        market=BrokerMarket.US_STOCK,
        symbol=symbol,
        now=NOW,
    )

    assert json.loads(preview.body)["symbol"] == expected


@pytest.mark.parametrize("symbol", ("NQ", "nq", "MNQ", "mnq"))
def test_preview_preserves_the_pre_extraction_unsupported_futures_roots(
    symbol: str,
) -> None:
    with pytest.raises(ValueError, match="unavailable"):
        adapter.build_toss_stock_order_preview(
            command=_command(),
            account_seq=17,
            market=BrokerMarket.US_STOCK,
            symbol=symbol,
            now=NOW,
        )
