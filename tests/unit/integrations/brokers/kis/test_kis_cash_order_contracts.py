from __future__ import annotations

import json
from decimal import Decimal

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.integrations.brokers.kis.cash_order_contracts import (
    KisCashAccount,
    KisCashEnvironment,
    KisCashOrderBusinessError,
    LockedOrderIntent,
    ProviderOrderIdentity,
    build_cash_cancel_request,
    build_cash_order_request,
    decode_cash_order_response,
)
from autotrader.shared.ids import new_uuid7


def _account(environment: KisCashEnvironment) -> KisCashAccount:
    return KisCashAccount(
        account_id=new_uuid7(),
        account_alias=(
            "kis-real-cash"
            if environment is KisCashEnvironment.LIVE
            else "kis-paper-cash"
        ),
        environment=environment,
        account_number="87654321",
        product_code="01",
        enabled=False,
    )


def _intent(**changes: object) -> LockedOrderIntent:
    intent = LockedOrderIntent(
        id=new_uuid7(),
        v6_decision_id=new_uuid7(),
        account_id=new_uuid7(),
        symbol="005930",
        side=Side.BUY,
        order_style=OrderStyle.LIMIT,
        quantity=Decimal("2"),
        limit_price=Decimal("70000"),
        opens_exposure=True,
        common_stock_authorized=True,
        binding_generation=1,
        locked=True,
    )
    values = {field: getattr(intent, field) for field in intent.__dataclass_fields__}
    values.update(changes)
    return LockedOrderIntent(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("environment", "side", "tr_id"),
    (
        (KisCashEnvironment.LIVE, Side.BUY, "TTTC0802U"),
        (KisCashEnvironment.LIVE, Side.SELL, "TTTC0801U"),
        (KisCashEnvironment.PAPER, Side.BUY, "VTTC0802U"),
        (KisCashEnvironment.PAPER, Side.SELL, "VTTC0801U"),
    ),
)
def test_order_uses_exact_environment_side_tr_id(
    environment: KisCashEnvironment,
    side: Side,
    tr_id: str,
) -> None:
    account = _account(environment)
    intent = _intent(account_id=account.account_id, side=side, opens_exposure=False)

    request = build_cash_order_request(intent, account)
    body = json.loads(request.body or b"")

    assert request.method == "POST"
    assert request.path == "/uapi/domestic-stock/v1/trading/order-cash"
    assert dict(request.headers) == {
        "content-type": "application/json; charset=utf-8",
        "custtype": "P",
        "tr_id": tr_id,
    }
    assert body == {
        "ACNT_PRDT_CD": "01",
        "CANO": "87654321",
        "CNDT_PRIC": "",
        "EXCG_ID_DVSN_CD": "KRX",
        "ORD_DVSN": "00",
        "ORD_QTY": "2",
        "ORD_UNPR": "70000",
        "PDNO": "005930",
        "SLL_TYPE": "01" if side is Side.SELL else "",
    }


def test_market_order_uses_zero_price_and_market_code() -> None:
    account = _account(KisCashEnvironment.PAPER)
    request = build_cash_order_request(
        _intent(
            account_id=account.account_id,
            order_style=OrderStyle.MARKET,
            limit_price=None,
        ),
        account,
    )

    body = json.loads(request.body or b"")
    assert body["ORD_DVSN"] == "01"
    assert body["ORD_UNPR"] == "0"


@pytest.mark.parametrize(
    ("changes", "match"),
    (
        ({"locked": False}, "locked"),
        ({"common_stock_authorized": False}, "common-stock"),
        ({"symbol": "AAPL"}, "symbol"),
        ({"quantity": Decimal("1.5")}, "integer"),
        ({"side": Side.SELL, "opens_exposure": True}, "short-opening"),
    ),
)
def test_invalid_or_unlocked_cash_intent_fails_closed(
    changes: dict[str, object],
    match: str,
) -> None:
    account = _account(KisCashEnvironment.PAPER)

    with pytest.raises(ValueError, match=match):
        build_cash_order_request(
            _intent(account_id=account.account_id, **changes),
            account,
        )


def test_account_scope_must_match_locked_intent() -> None:
    account = _account(KisCashEnvironment.PAPER)

    with pytest.raises(ValueError, match="account scope"):
        build_cash_order_request(_intent(), account)


def test_success_response_requires_all_provider_identity_fields() -> None:
    acknowledgement = decode_cash_order_response(
        {
            "rt_cd": "0",
            "msg_cd": "APBK0013",
            "msg1": "accepted",
            "output": {
                "KRX_FWDG_ORD_ORGNO": "06010",
                "ODNO": "0000012345",
                "ORD_TMD": "091501",
            },
        }
    )

    assert acknowledgement.organization_number == "06010"
    assert acknowledgement.order_number == "0000012345"
    assert acknowledgement.order_time == "091501"

    with pytest.raises(ValueError, match="malformed"):
        decode_cash_order_response(
            {
                "rt_cd": "0",
                "msg_cd": "APBK0013",
                "msg1": "accepted",
                "output": {"ODNO": "0000012345"},
            }
        )


def test_provider_business_failure_exposes_only_safe_message_code() -> None:
    with pytest.raises(KisCashOrderBusinessError, match="APBK0999") as caught:
        decode_cash_order_response(
            {
                "rt_cd": "1",
                "msg_cd": "APBK0999",
                "msg1": "account 87654321 rejected",
                "output": {},
            }
        )

    assert "87654321" not in str(caught.value)


@pytest.mark.parametrize(
    ("environment", "tr_id"),
    (
        (KisCashEnvironment.LIVE, "TTTC0803U"),
        (KisCashEnvironment.PAPER, "VTTC0803U"),
    ),
)
def test_cancel_uses_exact_identity_and_full_remaining_quantity(
    environment: KisCashEnvironment,
    tr_id: str,
) -> None:
    account = _account(environment)
    identity = ProviderOrderIdentity(
        organization_number="06010",
        order_number="0000012345",
        symbol="005930",
        side=Side.BUY,
        remaining_quantity=Decimal("2"),
        order_style=OrderStyle.LIMIT,
        limit_price=Decimal("70000"),
    )

    request = build_cash_cancel_request(identity, account)
    body = json.loads(request.body or b"")

    assert request.path == "/uapi/domestic-stock/v1/trading/order-rvsecncl"
    assert dict(request.headers)["tr_id"] == tr_id
    assert body == {
        "ACNT_PRDT_CD": "01",
        "CANO": "87654321",
        "EXCG_ID_DVSN_CD": "KRX",
        "KRX_FWDG_ORD_ORGNO": "06010",
        "ORD_DVSN": "00",
        "ORD_QTY": "2",
        "ORD_UNPR": "0",
        "ORGN_ODNO": "0000012345",
        "QTY_ALL_ORD_YN": "Y",
        "RVSE_CNCL_DVSN_CD": "02",
    }
