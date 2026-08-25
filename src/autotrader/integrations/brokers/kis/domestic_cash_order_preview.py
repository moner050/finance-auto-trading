from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.orders.models import BrokerOrderCommand, CommandType
from autotrader.shared.decimal import decimal_to_string, require_decimal

_KIS_DOMESTIC_CASH_ORDER_PATH = "/uapi/domestic-stock/v1/trading/order-cash"


class KisDomesticCashOrderEnvironment(StrEnum):
    REAL = "REAL"
    PAPER = "PAPER"


class KisDomesticCashOrderPreviewError(ValueError):
    """Safe public failure for KIS cash-order preview data."""


@dataclass(frozen=True, slots=True)
class KisDomesticCashOrderAccount:
    """Account scope needed to build, but not transmit, a KIS cash order."""

    account_number: str
    product_code: str

    def __post_init__(self) -> None:
        if not _digits(self.account_number, length=8) or not _digits(
            self.product_code, length=2
        ):
            raise ValueError("KIS domestic cash order account is invalid")


@dataclass(frozen=True, slots=True)
class KisDomesticCashOrderPreview:
    """Exact KIS cash-order request payload without credentials or transport."""

    path: str
    tr_id: str
    body: bytes

    def __post_init__(self) -> None:
        if self.path != _KIS_DOMESTIC_CASH_ORDER_PATH:
            raise ValueError("KIS domestic cash order preview path is invalid")
        if (
            self.tr_id
            not in {
                "TTTC0011U",
                "TTTC0012U",
                "VTTC0011U",
                "VTTC0012U",
            }
            or not self.body
        ):
            raise ValueError("KIS domestic cash order preview is invalid")


@dataclass(frozen=True, slots=True)
class KisDomesticCashOrderAcknowledgement:
    exchange_order_organization: str
    order_number: str
    order_time: str

    def __post_init__(self) -> None:
        if not _digits(self.exchange_order_organization, length=5):
            raise ValueError("KIS cash order exchange organization is invalid")
        if not _digits(self.order_number, length=10):
            raise ValueError("KIS cash order number is invalid")
        if not _digits(self.order_time, length=6):
            raise ValueError("KIS cash order time is invalid")


def build_kis_domestic_cash_order_preview(
    *,
    command: BrokerOrderCommand,
    account: KisDomesticCashOrderAccount,
    environment: KisDomesticCashOrderEnvironment,
    symbol: str,
    now: datetime,
) -> KisDomesticCashOrderPreview:
    """Build an official KIS KRX cash limit-order payload without a network call."""
    try:
        return _build_kis_domestic_cash_order_preview(
            command=command,
            account=account,
            environment=environment,
            symbol=symbol,
            now=now,
        )
    except Exception as caught:
        _scrub_error(caught)
        del caught, command, account, environment, symbol, now
        raise KisDomesticCashOrderPreviewError(
            "KIS domestic cash order preview is unavailable"
        ) from None


def _build_kis_domestic_cash_order_preview(
    *,
    command: BrokerOrderCommand,
    account: KisDomesticCashOrderAccount,
    environment: KisDomesticCashOrderEnvironment,
    symbol: str,
    now: datetime,
) -> KisDomesticCashOrderPreview:
    if command.command_type is not CommandType.SUBMIT:
        raise ValueError("KIS domestic cash preview requires a submit command")
    if (
        command.target_broker_order_id is not None
        or command.replaces_command_id is not None
    ):
        raise ValueError("KIS domestic cash submit cannot target an existing order")
    if not _is_utc(now) or not _is_utc(command.not_after) or now >= command.not_after:
        raise ValueError("KIS domestic cash preview command is expired")
    if command.time_in_force != "DAY" or command.order_style is not OrderStyle.LIMIT:
        raise ValueError("KIS domestic cash preview supports DAY limit orders only")
    if type(account) is not KisDomesticCashOrderAccount:
        raise ValueError("KIS domestic cash order account is invalid")
    if not _digits(symbol, length=6):
        raise ValueError("KIS domestic cash order symbol is invalid")
    quantity = _positive_integral(command.quantity, "quantity")
    price = _positive_integral(command.limit_price, "limit_price")
    tr_id, sell_type = _order_side(command.side, environment)
    return KisDomesticCashOrderPreview(
        path=_KIS_DOMESTIC_CASH_ORDER_PATH,
        tr_id=tr_id,
        body=json.dumps(
            {
                "CANO": account.account_number,
                "ACNT_PRDT_CD": account.product_code,
                "PDNO": symbol,
                "ORD_DVSN": "00",
                "ORD_QTY": decimal_to_string(quantity),
                "ORD_UNPR": decimal_to_string(price),
                "EXCG_ID_DVSN_CD": "KRX",
                "SLL_TYPE": sell_type,
                "CNDT_PRIC": "",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def decode_kis_domestic_cash_order_acknowledgement(
    *, status: int, body: bytes
) -> KisDomesticCashOrderAcknowledgement:
    """Decode only the documented successful KIS cash-order identifiers."""
    try:
        return _decode_kis_domestic_cash_order_acknowledgement(status=status, body=body)
    except Exception as caught:
        _scrub_error(caught)
        del caught, status, body
        raise KisDomesticCashOrderPreviewError(
            "KIS domestic cash order acknowledgement is unavailable"
        ) from None


def _decode_kis_domestic_cash_order_acknowledgement(
    *, status: int, body: bytes
) -> KisDomesticCashOrderAcknowledgement:
    if type(status) is not int or status != 200 or type(body) is not bytes:
        raise ValueError("KIS domestic cash order acknowledgement is invalid")
    payload = _decode_json_object(body)
    if not isinstance(payload, dict):
        raise ValueError("KIS domestic cash order acknowledgement is invalid")
    typed_payload = cast(dict[str, object], payload)
    if typed_payload.get("rt_cd") != "0":
        raise ValueError("KIS domestic cash order acknowledgement is invalid")
    output = typed_payload.get("output")
    if not isinstance(output, dict):
        raise ValueError("KIS domestic cash order acknowledgement is invalid")
    typed_output = cast(dict[str, object], output)
    return KisDomesticCashOrderAcknowledgement(
        exchange_order_organization=_response_digits(
            typed_output.get("KRX_FWDG_ORD_ORGNO"), length=5
        ),
        order_number=_response_digits(typed_output.get("ODNO"), length=10),
        order_time=_response_digits(typed_output.get("ORD_TMD"), length=6),
    )


def _digits(value: object, *, length: int) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and value.isascii()
        and value.isdecimal()
    )


def _is_utc(value: object) -> bool:
    return (
        type(value) is datetime
        and value.tzinfo is UTC
        and value.utcoffset() == UTC.utcoffset(value)
    )


def _positive_integral(value: object, name: str) -> Decimal:
    decimal = require_decimal(value)
    if decimal <= 0 or decimal != decimal.to_integral_value():
        raise ValueError(f"KIS domestic cash order {name} must be a positive integer")
    return decimal


def _order_side(
    side: object, environment: KisDomesticCashOrderEnvironment
) -> tuple[str, str]:
    if type(environment) is not KisDomesticCashOrderEnvironment:
        raise ValueError("KIS domestic cash order environment is invalid")
    prefix = "TTTC" if environment is KisDomesticCashOrderEnvironment.REAL else "VTTC"
    if side is Side.BUY:
        return f"{prefix}0012U", ""
    if side is Side.SELL:
        return f"{prefix}0011U", "01"
    raise ValueError("KIS domestic cash order side is invalid")


def _response_digits(value: object, *, length: int) -> str:
    if not _digits(value, length=length):
        raise ValueError("KIS domestic cash order acknowledgement is invalid")
    return cast(str, value)


def _decode_json_object(body: bytes) -> object | None:
    try:
        return json.loads(body)
    except UnicodeDecodeError, json.JSONDecodeError:
        return None


def _scrub_error(caught: Exception) -> None:
    caught.__traceback__ = None
    caught.__context__ = None
    caught.__cause__ = None
    caught.args = ()
    caught.__dict__.clear()
