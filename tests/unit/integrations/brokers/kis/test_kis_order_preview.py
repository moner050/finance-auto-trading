from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid7

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.common import BrokerResponse
from autotrader.integrations.brokers.kis.adapter import (
    KisAccountReadCredentials,
    build_kis_futures_order_preview,
    decode_kis_futures_order_acknowledgement,
)
from autotrader.integrations.brokers.kis.contracts import KisActiveContract

NOW = datetime(2026, 8, 10, tzinfo=UTC)


def command(**overrides: object) -> BrokerOrderCommand:
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
        "broker_client_order_id": f"client-{uuid7().hex}",
        "target_broker_order_id": None,
        "replaces_command_id": None,
        "origin_type": "STRATEGY",
        "authority_class": "SUBMIT_NEW_EXPOSURE",
        "owner_runtime_instance_id": uuid7(),
        "fencing_token": 1,
        "not_after": NOW + timedelta(minutes=1),
        "side": Side.BUY,
        "order_style": OrderStyle.MARKET,
        "quantity": Decimal("2"),
        "limit_price": None,
        "time_in_force": "DAY",
    }
    values.update(overrides)
    return BrokerOrderCommand(**values)  # type: ignore[arg-type]


def account() -> KisAccountReadCredentials:
    return KisAccountReadCredentials(
        access_token="token",
        app_key="app",
        app_secret="secret",
        account_number="81012345",
        product_code="08",
    )


def contract(instrument_id: object) -> KisActiveContract:
    return KisActiveContract(
        evidence_id=uuid7(),
        data_source_id=uuid7(),
        instrument_id=instrument_id,
        provider_contract_code="NQZ26",
        provider_exchange_code="CME",
        expires_at=NOW + timedelta(hours=1),
    )


def test_kis_preview_builds_the_documented_day_market_order_payload() -> None:
    request = command()
    preview = build_kis_futures_order_preview(
        command=request,
        account=account(),
        contract=contract(request.instrument_id),
        now=NOW,
    )

    assert preview.tr_id == "OTFM3001U"
    assert json.loads(preview.body) == {
        "ACNT_PRDT_CD": "08",
        "CANO": "81012345",
        "CCLD_CNDT_CD": "2",
        "CPLX_ORD_DVSN_CD": "0",
        "ECIS_RSVN_ORD_YN": "N",
        "FM_HDGE_ORD_SCRN_YN": "N",
        "FM_LIMIT_ORD_PRIC": "",
        "FM_LQD_LMT_ORD_PRIC": "",
        "FM_LQD_STOP_ORD_PRIC": "",
        "FM_LQD_USTL_CCLD_DT": "",
        "FM_LQD_USTL_CCNO": "",
        "FM_ORD_QTY": "2",
        "FM_STOP_ORD_PRIC": "",
        "OVRS_FUTR_FX_PDNO": "NQZ26",
        "PRIC_DVSN_CD": "2",
        "SLL_BUY_DVSN_CD": "02",
    }


def test_kis_preview_rejects_a_mismatched_contract_instrument() -> None:
    with pytest.raises(ValueError, match="instrument"):
        build_kis_futures_order_preview(
            command=command(),
            account=account(),
            contract=contract(uuid7()),
            now=NOW,
        )


def test_kis_preview_rejects_an_expired_safety_kernel_command() -> None:
    request = command(not_after=NOW)

    with pytest.raises(ValueError, match="not_after"):
        build_kis_futures_order_preview(
            command=request,
            account=account(),
            contract=contract(request.instrument_id),
            now=NOW,
        )


def test_kis_order_acknowledgement_preserves_the_local_order_date_and_number() -> None:
    acknowledgement = decode_kis_futures_order_acknowledgement(
        BrokerResponse(
            status=200,
            body=b'{"rt_cd":"0","output":{"ORD_DT":"20260810","ODNO":"00360686"}}',
        )
    )

    assert acknowledgement.local_order_date == "20260810"
    assert acknowledgement.order_number == "00360686"
