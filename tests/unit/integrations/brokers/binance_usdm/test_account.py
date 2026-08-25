from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from autotrader.integrations.brokers.binance_usdm.account import (
    BinanceUsdmAccountCaptureError,
    capture_binance_usdm_account,
)
from autotrader.integrations.brokers.common import BrokerRequest, BrokerResponse

AS_OF = datetime(2026, 8, 24, 15, 30, tzinfo=UTC)


def _response(payload: object, *, status: int = 200) -> BrokerResponse:
    return BrokerResponse(status=status, body=json.dumps(payload).encode())


@dataclass
class Reader:
    payloads: dict[str, BrokerResponse]
    requests: list[BrokerRequest] = field(default_factory=lambda: [])

    async def send(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return self.payloads[request.path.partition("?")[0]]


def complete_payloads() -> dict[str, BrokerResponse]:
    return {
        "/fapi/v3/balance": _response(
            [
                {
                    "asset": "USDT",
                    "balance": "1000",
                    "availableBalance": "970",
                    "maxWithdrawAmount": "970",
                    "updateTime": 1787585399000,
                }
            ]
        ),
        "/fapi/v3/positionRisk": _response(
            [
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": "0.01",
                    "entryPrice": "60000",
                    "markPrice": "60100",
                    "unRealizedProfit": "1",
                    "isolatedMargin": "90",
                    "notional": "601",
                    "marginAsset": "USDT",
                    "initialMargin": "100",
                    "maintMargin": "3",
                    "positionInitialMargin": "100",
                    "openOrderInitialMargin": "5",
                    "updateTime": 1787585399000,
                }
            ]
        ),
        "/fapi/v1/openOrders": _response(
            [
                {
                    "orderId": 11,
                    "clientOrderId": "owned-entry-1",
                    "symbol": "BTCUSDT",
                    "status": "NEW",
                    "side": "BUY",
                    "type": "LIMIT",
                    "executedQty": "0",
                    "origQty": "0.01",
                    "reduceOnly": False,
                    "closePosition": False,
                }
            ]
        ),
        "/fapi/v1/openAlgoOrders": _response(
            [
                {
                    "algoId": 12,
                    "clientAlgoId": "owned-stop-1",
                    "symbol": "BTCUSDT",
                    "algoStatus": "NEW",
                    "side": "SELL",
                    "orderType": "STOP_MARKET",
                    "quantity": "0",
                    "triggerPrice": "59000",
                    "closePosition": True,
                }
            ]
        ),
        "/fapi/v1/allOrders": _response(
            [
                {
                    "orderId": 11,
                    "clientOrderId": "owned-entry-1",
                    "symbol": "BTCUSDT",
                    "status": "NEW",
                    "side": "BUY",
                    "type": "LIMIT",
                    "executedQty": "0",
                    "origQty": "0.01",
                    "reduceOnly": False,
                    "closePosition": False,
                }
            ]
        ),
        "/fapi/v1/allAlgoOrders": _response(
            [
                {
                    "algoId": 12,
                    "clientAlgoId": "owned-stop-1",
                    "symbol": "BTCUSDT",
                    "algoStatus": "NEW",
                    "side": "SELL",
                    "orderType": "STOP_MARKET",
                    "quantity": "0",
                    "triggerPrice": "59000",
                    "closePosition": True,
                }
            ]
        ),
        "/fapi/v1/userTrades": _response(
            [
                {
                    "id": 21,
                    "orderId": 10,
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "qty": "0.01",
                    "price": "60000",
                    "commission": "0.24",
                    "commissionAsset": "USDT",
                    "realizedPnl": "0",
                    "time": 1787585300000,
                }
            ]
        ),
        "/fapi/v1/income": _response(
            [
                {
                    "symbol": "BTCUSDT",
                    "incomeType": "FUNDING_FEE",
                    "income": "-0.10",
                    "asset": "USDT",
                    "time": 1787585350000,
                    "tranId": 31,
                    "tradeId": "21",
                }
            ]
        ),
    }


@pytest.mark.asyncio
async def test_captures_wallet_equity_margin_exposure_orders_fills_and_income() -> None:
    reader = Reader(complete_payloads())

    snapshot = await capture_binance_usdm_account(reader=reader, as_of=AS_OF)

    assert snapshot.as_of == AS_OF
    assert snapshot.usdt_wallet_balance == Decimal("1000")
    assert snapshot.usdt_available_balance == Decimal("970")
    assert snapshot.usdt_equity == Decimal("1001")
    assert snapshot.initial_margin == Decimal("105")
    assert snapshot.maintenance_margin == Decimal("3")
    assert snapshot.positions[0].symbol == "BTCUSDT"
    assert snapshot.positions[0].amount == Decimal("0.01")
    assert snapshot.normal_orders[0].client_order_id == "owned-entry-1"
    assert snapshot.algo_orders[0].client_algo_id == "owned-stop-1"
    assert snapshot.trades[0].commission == Decimal("0.24")
    assert snapshot.income[0].income_type == "FUNDING_FEE"
    assert [request.path.partition("?")[0] for request in reader.requests] == [
        "/fapi/v3/balance",
        "/fapi/v3/positionRisk",
        "/fapi/v1/openOrders",
        "/fapi/v1/openAlgoOrders",
        "/fapi/v1/allOrders",
        "/fapi/v1/allAlgoOrders",
        "/fapi/v1/userTrades",
        "/fapi/v1/income",
    ]
    assert all(request.method == "GET" for request in reader.requests)
    for index in (4, 5, 6):
        assert "symbol=BTCUSDT" in reader.requests[index].path
    for index in (4, 5, 6, 7):
        assert "startTime=" in reader.requests[index].path
        assert "endTime=" in reader.requests[index].path
        assert "limit=1000" in reader.requests[index].path


@pytest.mark.asyncio
async def test_preserves_unexpected_symbol_exposure_for_fail_closed_verification() -> (
    None
):
    payloads = complete_payloads()
    payloads["/fapi/v3/positionRisk"] = _response(
        [
            {
                "symbol": "ETHUSDT",
                "positionSide": "BOTH",
                "positionAmt": "1",
                "entryPrice": "3000",
                "markPrice": "3010",
                "unRealizedProfit": "10",
                "isolatedMargin": "500",
                "notional": "3010",
                "marginAsset": "USDT",
                "initialMargin": "500",
                "maintMargin": "15",
                "positionInitialMargin": "500",
                "openOrderInitialMargin": "0",
                "updateTime": 1787585399000,
            }
        ]
    )

    snapshot = await capture_binance_usdm_account(
        reader=Reader(payloads),
        as_of=AS_OF,
    )

    assert snapshot.positions[0].symbol == "ETHUSDT"
    assert snapshot.positions[0].amount == Decimal("1")


@pytest.mark.asyncio
async def test_preserves_unexpected_symbol_open_order() -> None:
    payloads = complete_payloads()
    unexpected = {
        **json.loads(payloads["/fapi/v1/openOrders"].body)[0],
        "orderId": 99,
        "clientOrderId": "unexpected-eth-order",
        "symbol": "ETHUSDT",
    }
    payloads["/fapi/v1/openOrders"] = _response([unexpected])

    snapshot = await capture_binance_usdm_account(
        reader=Reader(payloads),
        as_of=AS_OF,
    )

    assert {order.symbol for order in snapshot.normal_orders} == {
        "BTCUSDT",
        "ETHUSDT",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "response"),
    (
        ("/fapi/v3/balance", BrokerResponse(500, b"private provider body")),
        ("/fapi/v3/positionRisk", _response([{"symbol": "BTCUSDT"}])),
        ("/fapi/v1/openOrders", _response("not-a-list")),
        ("/fapi/v1/openAlgoOrders", _response([{"algoId": "bad"}])),
        ("/fapi/v1/allOrders", _response("not-a-list")),
        ("/fapi/v1/allAlgoOrders", _response([{"algoId": "bad"}])),
        ("/fapi/v1/userTrades", _response([{"id": 1}, {"id": 1}])),
        ("/fapi/v1/income", _response([{"tranId": True}])),
    ),
)
async def test_incomplete_provider_fact_fails_without_raw_body_leak(
    path: str,
    response: BrokerResponse,
) -> None:
    payloads = complete_payloads()
    payloads[path] = response

    with pytest.raises(BinanceUsdmAccountCaptureError) as raised:
        await capture_binance_usdm_account(reader=Reader(payloads), as_of=AS_OF)

    assert "private" not in repr(raised.value)


@pytest.mark.asyncio
async def test_refuses_a_history_page_that_could_be_truncated() -> None:
    payloads = complete_payloads()
    order = json.loads(payloads["/fapi/v1/allOrders"].body)[0]
    payloads["/fapi/v1/allOrders"] = _response(
        [
            {
                **order,
                "orderId": index + 1,
                "clientOrderId": f"owned-entry-{index + 1}",
            }
            for index in range(1000)
        ]
    )

    with pytest.raises(BinanceUsdmAccountCaptureError):
        await capture_binance_usdm_account(reader=Reader(payloads), as_of=AS_OF)


@pytest.mark.asyncio
async def test_rejects_non_utc_capture_before_any_read() -> None:
    reader = Reader(complete_payloads())

    with pytest.raises(ValueError, match="UTC"):
        await capture_binance_usdm_account(
            reader=reader,
            as_of=datetime(2026, 8, 24, 15, 30),
        )

    assert reader.requests == []
