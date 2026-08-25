from __future__ import annotations

import json
import subprocess
import sys
import traceback
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import FrameType, TracebackType
from typing import cast
from uuid import uuid7

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.integrations.brokers.kis.domestic_cash_order_preview import (
    KisDomesticCashOrderAccount,
    KisDomesticCashOrderAcknowledgement,
    KisDomesticCashOrderEnvironment,
    build_kis_domestic_cash_order_preview,
    decode_kis_domestic_cash_order_acknowledgement,
)

NOW = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)


def _command(**changes: object) -> BrokerOrderCommand:
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
        "order_style": OrderStyle.LIMIT,
        "quantity": Decimal("2"),
        "limit_price": Decimal("70000"),
        "time_in_force": "DAY",
    }
    values.update(changes)
    return BrokerOrderCommand(**values)  # type: ignore[arg-type]


def _account() -> KisDomesticCashOrderAccount:
    return KisDomesticCashOrderAccount(account_number="81012345", product_code="01")


def test_kis_cash_preview_builds_official_limit_buy_payload() -> None:
    preview = build_kis_domestic_cash_order_preview(
        command=_command(),
        account=_account(),
        environment=KisDomesticCashOrderEnvironment.REAL,
        symbol="005930",
        now=NOW,
    )

    assert preview.path == "/uapi/domestic-stock/v1/trading/order-cash"
    assert preview.tr_id == "TTTC0012U"
    assert json.loads(preview.body) == {
        "ACNT_PRDT_CD": "01",
        "CANO": "81012345",
        "CNDT_PRIC": "",
        "EXCG_ID_DVSN_CD": "KRX",
        "ORD_DVSN": "00",
        "ORD_QTY": "2",
        "ORD_UNPR": "70000",
        "PDNO": "005930",
        "SLL_TYPE": "",
    }


def test_kis_cash_preview_builds_official_limit_sell_payload() -> None:
    preview = build_kis_domestic_cash_order_preview(
        command=_command(side=Side.SELL),
        account=_account(),
        environment=KisDomesticCashOrderEnvironment.REAL,
        symbol="005930",
        now=NOW,
    )

    assert preview.tr_id == "TTTC0011U"
    assert json.loads(preview.body)["SLL_TYPE"] == "01"


@pytest.mark.parametrize(
    ("environment", "side", "expected_tr_id"),
    (
        (KisDomesticCashOrderEnvironment.REAL, Side.BUY, "TTTC0012U"),
        (KisDomesticCashOrderEnvironment.REAL, Side.SELL, "TTTC0011U"),
        (KisDomesticCashOrderEnvironment.PAPER, Side.BUY, "VTTC0012U"),
        (KisDomesticCashOrderEnvironment.PAPER, Side.SELL, "VTTC0011U"),
    ),
)
def test_kis_cash_preview_uses_explicit_environment_tr_id(
    environment: KisDomesticCashOrderEnvironment,
    side: Side,
    expected_tr_id: str,
) -> None:
    preview = build_kis_domestic_cash_order_preview(
        command=_command(side=side),
        account=_account(),
        environment=environment,
        symbol="005930",
        now=NOW,
    )

    assert preview.tr_id == expected_tr_id


@pytest.mark.parametrize(
    "changes",
    (
        {"order_style": OrderStyle.MARKET, "limit_price": None},
        {"quantity": Decimal("1.5")},
        {"limit_price": Decimal("70000.5")},
        {"not_after": NOW},
        {"command_type": CommandType.CANCEL},
    ),
)
def test_kis_cash_preview_rejects_non_cash_limit_submit(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        build_kis_domestic_cash_order_preview(
            command=_command(**changes),
            account=_account(),
            environment=KisDomesticCashOrderEnvironment.REAL,
            symbol="005930",
            now=NOW,
        )


@pytest.mark.parametrize(
    "changes",
    (
        {"not_after": NOW.replace(tzinfo=None)},
        {"target_broker_order_id": "existing-order"},
        {"replaces_command_id": uuid7()},
        {"side": object()},
    ),
)
def test_kis_cash_preview_rejects_non_new_or_non_utc_command(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        build_kis_domestic_cash_order_preview(
            command=_command(**changes),
            account=_account(),
            environment=KisDomesticCashOrderEnvironment.REAL,
            symbol="005930",
            now=NOW,
        )


@pytest.mark.parametrize(
    ("account", "symbol", "now"),
    (
        (KisDomesticCashOrderAccount("81012345", "01"), "00593A", NOW),
        (
            KisDomesticCashOrderAccount("81012345", "01"),
            "005930",
            NOW.replace(tzinfo=None),
        ),
    ),
)
def test_kis_cash_preview_rejects_invalid_symbol_or_non_utc_now(
    account: KisDomesticCashOrderAccount, symbol: str, now: datetime
) -> None:
    with pytest.raises(ValueError):
        build_kis_domestic_cash_order_preview(
            command=_command(),
            account=account,
            environment=KisDomesticCashOrderEnvironment.REAL,
            symbol=symbol,
            now=now,
        )


def test_kis_cash_order_acknowledgement_decodes_only_successful_provider_shape() -> (
    None
):
    acknowledgement = decode_kis_domestic_cash_order_acknowledgement(
        status=200,
        body=(
            b'{"rt_cd":"0","output":{"KRX_FWDG_ORD_ORGNO":"91234",'
            b'"ODNO":"0001569157","ORD_TMD":"090001"}}'
        ),
    )

    assert acknowledgement == KisDomesticCashOrderAcknowledgement(
        exchange_order_organization="91234",
        order_number="0001569157",
        order_time="090001",
    )


@pytest.mark.parametrize(
    ("status", "body"),
    (
        (500, b"{}"),
        (200, b"not-json"),
        (200, b'{"rt_cd":"1","output":{}}'),
        (200, b'{"rt_cd":"0","output":{"ODNO":"invalid"}}'),
        (
            200,
            b'{"rt_cd":"0","output":{"KRX_FWDG_ORD_ORGNO":"91234",'
            b'"ODNO":"00360686","ORD_TMD":"090001"}}',
        ),
    ),
)
def test_kis_cash_order_acknowledgement_fails_closed(status: int, body: bytes) -> None:
    with pytest.raises(ValueError):
        decode_kis_domestic_cash_order_acknowledgement(status=status, body=body)


def test_kis_preview_public_error_trace_does_not_retain_sensitive_input() -> None:
    secret = "private-account-response-body"
    with pytest.raises(ValueError) as caught:
        decode_kis_domestic_cash_order_acknowledgement(
            status=200, body=secret.encode("utf-8")
        )

    rendered = "".join(traceback.format_exception(caught.value))
    assert secret not in rendered


def test_kis_preview_public_error_traceback_graph_does_not_retain_builder_inputs() -> (
    None
):
    error, forbidden_ids = _capture_kis_builder_error()

    assert not _traceback_reaches_forbidden_value(error, forbidden_ids)


def test_kis_preview_public_error_traceback_graph_does_not_retain_response_body() -> (
    None
):
    error, forbidden_ids = _capture_kis_decoder_error()

    assert not _traceback_reaches_forbidden_value(error, forbidden_ids)


def test_kis_cash_account_rejects_malformed_values() -> None:
    with pytest.raises(ValueError):
        KisDomesticCashOrderAccount(account_number="8101234A", product_code="01")


def test_kis_cash_preview_fresh_import_loads_no_transport_or_runtime() -> None:
    code = """
import sys
import autotrader.integrations.brokers.kis.domestic_cash_order_preview
prefixes = (
    'autotrader.apps', 'autotrader.config', 'autotrader.persistence',
    'autotrader.operations', 'autotrader.integrations.brokers.common',
    'autotrader.integrations.brokers.kis.domestic_cash',
)
loaded = sorted(
    name
    for name in sys.modules
    if name.startswith(prefixes)
    and name != 'autotrader.integrations.brokers.kis.domestic_cash_order_preview'
)
if loaded:
    raise SystemExit(';'.join(loaded))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def _capture_kis_builder_error() -> tuple[BaseException, frozenset[int]]:
    command = _command(broker_client_order_id="private-kis-client")
    account = KisDomesticCashOrderAccount("81012345", "01")
    symbol = "00593A"
    now = NOW
    forbidden_ids = frozenset(map(id, (command, account, symbol, now)))
    try:
        build_kis_domestic_cash_order_preview(
            command=command,
            account=account,
            environment=KisDomesticCashOrderEnvironment.REAL,
            symbol=symbol,
            now=now,
        )
    except ValueError as error:
        del command, account, symbol, now
        return error, forbidden_ids
    raise AssertionError("KIS invalid builder input must fail")


def _capture_kis_decoder_error() -> tuple[BaseException, frozenset[int]]:
    body = b'{"private-kis-response":"secret"}'
    forbidden_ids = frozenset({id(body)})
    try:
        decode_kis_domestic_cash_order_acknowledgement(status=200, body=body)
    except ValueError as error:
        del body
        return error, forbidden_ids
    raise AssertionError("KIS invalid acknowledgement must fail")


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
