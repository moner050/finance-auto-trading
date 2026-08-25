from __future__ import annotations

import json
import traceback
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import FrameType, TracebackType
from typing import cast
from uuid import uuid7

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.common import BrokerMarket, BrokerResponse
from autotrader.integrations.brokers.toss.adapter import (
    build_toss_stock_order_preview,
    decode_toss_order_submission_acknowledgement,
)

NOW = datetime(2026, 8, 11, tzinfo=UTC)


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


def test_toss_preview_builds_an_idempotent_account_scoped_krx_limit_order() -> None:
    request = command()

    preview = build_toss_stock_order_preview(
        command=request,
        account_seq=17,
        market=BrokerMarket.KRX_STOCK,
        symbol="005930",
        now=NOW,
    )

    assert preview.account_seq == "17"
    assert json.loads(preview.body) == {
        "clientOrderId": request.broker_client_order_id,
        "confirmHighValueOrder": False,
        "orderType": "LIMIT",
        "price": "70000",
        "quantity": "2",
        "side": "BUY",
        "symbol": "005930",
        "timeInForce": "DAY",
    }


def test_toss_preview_rejects_fractional_krx_quantity_before_any_transport() -> None:
    with pytest.raises(ValueError, match="unavailable"):
        build_toss_stock_order_preview(
            command=command(quantity=Decimal("1.5")),
            account_seq=17,
            market=BrokerMarket.KRX_STOCK,
            symbol="005930",
            now=NOW,
        )


def test_toss_preview_rejects_a_non_ascii_client_order_id() -> None:
    with pytest.raises(ValueError, match="unavailable"):
        build_toss_stock_order_preview(
            command=command(broker_client_order_id="주문"),
            account_seq=17,
            market=BrokerMarket.KRX_STOCK,
            symbol="005930",
            now=NOW,
        )


def test_toss_preview_public_error_trace_does_not_retain_sensitive_input() -> None:
    secret = "private-account-symbol"
    with pytest.raises(ValueError) as caught:
        build_toss_stock_order_preview(
            command=command(broker_client_order_id=secret),
            account_seq=secret,
            market=BrokerMarket.KRX_STOCK,
            symbol=secret,
            now=NOW,
        )

    rendered = "".join(traceback.format_exception(caught.value))
    assert secret not in rendered


def test_toss_preview_public_error_traceback_graph_does_not_retain_builder_inputs() -> (
    None
):
    error, forbidden_ids = _capture_toss_builder_error()

    assert not _traceback_reaches_forbidden_value(error, forbidden_ids)


def test_toss_market_preview_omits_the_forbidden_limit_price_field() -> None:
    request = command(order_style=OrderStyle.MARKET, limit_price=None)

    preview = build_toss_stock_order_preview(
        command=request,
        account_seq=17,
        market=BrokerMarket.US_STOCK,
        symbol="AAPL",
        now=NOW,
    )

    assert json.loads(preview.body) == {
        "clientOrderId": request.broker_client_order_id,
        "confirmHighValueOrder": False,
        "orderType": "MARKET",
        "quantity": "2",
        "side": "BUY",
        "symbol": "AAPL",
        "timeInForce": "DAY",
    }


def test_toss_submission_acknowledgement_requires_the_preview_client_order_id() -> None:
    request = command()
    preview = build_toss_stock_order_preview(
        command=request,
        account_seq=17,
        market=BrokerMarket.KRX_STOCK,
        symbol="005930",
        now=NOW,
    )

    acknowledgement = decode_toss_order_submission_acknowledgement(
        BrokerResponse(
            status=200,
            body=(
                b'{"result":{"orderId":"provider-order-1","clientOrderId":"'
                + request.broker_client_order_id.encode("ascii")
                + b'"}}'
            ),
        ),
        preview=preview,
    )

    assert acknowledgement.order_id == "provider-order-1"
    assert acknowledgement.client_order_id == request.broker_client_order_id


def test_toss_preview_traceback_does_not_retain_response_or_preview() -> None:
    error, forbidden_ids = _capture_toss_decoder_error()

    assert not _traceback_reaches_forbidden_value(error, forbidden_ids)


def _capture_toss_builder_error() -> tuple[BaseException, frozenset[int]]:
    request = command(broker_client_order_id="private-toss-client")
    account_seq = "private-account-sequence"
    symbol = "private-symbol"
    forbidden_ids = frozenset(map(id, (request, account_seq, symbol)))
    try:
        build_toss_stock_order_preview(
            command=request,
            account_seq=account_seq,
            market=BrokerMarket.KRX_STOCK,
            symbol=symbol,
            now=NOW,
        )
    except ValueError as error:
        del request, account_seq, symbol
        return error, forbidden_ids
    raise AssertionError("Toss invalid builder input must fail")


def _capture_toss_decoder_error() -> tuple[BaseException, frozenset[int]]:
    request = command()
    preview = build_toss_stock_order_preview(
        command=request,
        account_seq=17,
        market=BrokerMarket.KRX_STOCK,
        symbol="005930",
        now=NOW,
    )
    response = BrokerResponse(status=200, body=b'{"private-toss-response":"secret"}')
    forbidden_ids = frozenset(map(id, (preview, response, response.body)))
    try:
        decode_toss_order_submission_acknowledgement(response, preview=preview)
    except ValueError as error:
        del request, preview, response
        return error, forbidden_ids
    raise AssertionError("Toss invalid acknowledgement must fail")


def _traceback_reaches_forbidden_value(
    error: BaseException, forbidden_ids: frozenset[int]
) -> bool:
    pending: list[object] = [error.__traceback__]
    seen: set[int] = set()
    while pending and len(seen) < 256:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if id(current) in forbidden_ids:
            return True
        if isinstance(current, BaseException):
            pending.extend(
                (
                    current.args,
                    current.__cause__,
                    current.__context__,
                    current.__traceback__,
                )
            )
        elif isinstance(current, TracebackType):
            pending.extend((current.tb_frame, current.tb_next))
        elif isinstance(current, tuple | list | frozenset):
            pending.extend(
                cast(tuple[object, ...] | list[object] | frozenset[object], current)
            )
        elif isinstance(current, dict):
            pending.extend(cast(dict[object, object], current).items())
        elif isinstance(current, FrameType):
            pending.extend(cast(dict[str, object], current.f_locals).values())
    return False
