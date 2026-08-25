from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from autotrader.domain.enums import OrderStyle, Side
from autotrader.integrations.brokers.common import BrokerRequest
from autotrader.shared.decimal import decimal_to_string, require_decimal

_ORDER_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
_CANCEL_PATH = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
_SAFE_CODE = re.compile(r"^[A-Z0-9_-]{1,32}$", re.ASCII)


class KisCashEnvironment(StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class KisCashOrderBusinessError(ValueError):
    """A redacted KIS business failure containing only a validated message code."""


@dataclass(frozen=True, slots=True)
class KisCashAccount:
    account_id: UUID
    account_alias: str
    environment: KisCashEnvironment
    account_number: str
    product_code: str
    enabled: bool

    def __post_init__(self) -> None:
        _require_uuid7(self.account_id, "account_id")
        expected_alias = (
            "kis-real-cash"
            if self.environment is KisCashEnvironment.LIVE
            else "kis-paper-cash"
        )
        if type(self.environment) is not KisCashEnvironment:
            raise TypeError("environment must be an exact KisCashEnvironment")
        if self.account_alias != expected_alias:
            raise ValueError("exact KIS cash account alias is required")
        if not _digits(self.account_number, 8) or not _digits(self.product_code, 2):
            raise ValueError("KIS cash account number shape is invalid")
        if type(self.enabled) is not bool or self.enabled:
            raise ValueError(
                "KIS cash contract construction requires a disabled account"
            )


@dataclass(frozen=True, slots=True)
class LockedOrderIntent:
    id: UUID
    v6_decision_id: UUID
    account_id: UUID
    symbol: str
    side: Side
    order_style: OrderStyle
    quantity: Decimal
    limit_price: Decimal | None
    opens_exposure: bool
    common_stock_authorized: bool
    binding_generation: int
    locked: bool

    def __post_init__(self) -> None:
        for name in ("id", "v6_decision_id", "account_id"):
            _require_uuid7(getattr(self, name), name)
        if not _digits(self.symbol, 6):
            raise ValueError("KRX symbol must contain six digits")
        if type(self.side) is not Side:
            raise TypeError("side must be an exact Side")
        if type(self.order_style) is not OrderStyle:
            raise TypeError("order_style must be an exact OrderStyle")
        quantity = _positive_integer(self.quantity, "quantity")
        object.__setattr__(self, "quantity", quantity)
        if self.order_style is OrderStyle.LIMIT:
            if self.limit_price is None:
                raise ValueError("limit intent requires a limit price")
            limit_price = _positive_integer(self.limit_price, "limit price")
            object.__setattr__(self, "limit_price", limit_price)
        elif self.limit_price is not None:
            raise ValueError("market intent cannot carry a limit price")
        if self.side is Side.SELL and self.opens_exposure:
            raise ValueError("KIS cash short-opening sell is unsupported")
        if not self.common_stock_authorized:
            raise ValueError("KRX common-stock authority is required")
        if type(self.binding_generation) is not int or self.binding_generation <= 0:
            raise ValueError("binding_generation must be positive")
        if type(self.locked) is not bool or not self.locked:
            raise ValueError("KIS cash intent must be locked")


@dataclass(frozen=True, slots=True)
class ProviderOrderIdentity:
    organization_number: str
    order_number: str
    symbol: str
    side: Side
    remaining_quantity: Decimal
    order_style: OrderStyle
    limit_price: Decimal | None

    def __post_init__(self) -> None:
        if not _digits(self.organization_number, 5):
            raise ValueError("provider organization number is invalid")
        if not _digits(self.order_number, 10):
            raise ValueError("provider order number is invalid")
        if not _digits(self.symbol, 6):
            raise ValueError("provider order symbol is invalid")
        if type(self.side) is not Side or type(self.order_style) is not OrderStyle:
            raise TypeError("provider order side and style must be exact enums")
        quantity = _positive_integer(self.remaining_quantity, "remaining quantity")
        object.__setattr__(self, "remaining_quantity", quantity)
        if self.order_style is OrderStyle.LIMIT:
            if self.limit_price is None:
                raise ValueError("limit provider order requires price")
            object.__setattr__(
                self,
                "limit_price",
                _positive_integer(self.limit_price, "limit price"),
            )
        elif self.limit_price is not None:
            raise ValueError("market provider order cannot carry price")


@dataclass(frozen=True, slots=True)
class KisCashOrderAck:
    organization_number: str
    order_number: str
    order_time: str
    message_code: str


class _AckOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=False)

    organization_number: str = Field(alias="KRX_FWDG_ORD_ORGNO")
    order_number: str = Field(alias="ODNO")
    order_time: str = Field(alias="ORD_TMD")

    @field_validator("organization_number")
    @classmethod
    def validate_organization(cls, value: str) -> str:
        if not _digits(value, 5):
            raise ValueError("invalid organization")
        return value

    @field_validator("order_number")
    @classmethod
    def validate_order_number(cls, value: str) -> str:
        if not _digits(value, 10):
            raise ValueError("invalid order number")
        return value

    @field_validator("order_time")
    @classmethod
    def validate_order_time(cls, value: str) -> str:
        if not _digits(value, 6):
            raise ValueError("invalid order time")
        return value


class _AckEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rt_cd: str
    msg_cd: str
    msg1: str
    output: _AckOutput


def build_cash_order_request(
    intent: LockedOrderIntent,
    account: KisCashAccount,
) -> BrokerRequest:
    if type(intent) is not LockedOrderIntent:
        raise TypeError("intent must be an exact LockedOrderIntent")
    if type(account) is not KisCashAccount:
        raise TypeError("account must be an exact KisCashAccount")
    intent.__post_init__()
    account.__post_init__()
    if intent.account_id != account.account_id:
        raise ValueError("KIS cash intent account scope does not match")
    tr_id = _order_tr_id(account.environment, intent.side)
    order_code = "00" if intent.order_style is OrderStyle.LIMIT else "01"
    price = Decimal(0) if intent.limit_price is None else intent.limit_price
    return _post_request(
        path=_ORDER_PATH,
        tr_id=tr_id,
        body={
            "CANO": account.account_number,
            "ACNT_PRDT_CD": account.product_code,
            "PDNO": intent.symbol,
            "ORD_DVSN": order_code,
            "ORD_QTY": decimal_to_string(intent.quantity),
            "ORD_UNPR": decimal_to_string(price),
            "EXCG_ID_DVSN_CD": "KRX",
            "SLL_TYPE": "01" if intent.side is Side.SELL else "",
            "CNDT_PRIC": "",
        },
    )


def decode_cash_order_response(payload: Mapping[str, object]) -> KisCashOrderAck:
    raw = dict(payload)
    result_code = raw.get("rt_cd")
    message_code = raw.get("msg_cd")
    if result_code != "0":
        safe_code = _safe_message_code(message_code)
        raise KisCashOrderBusinessError(f"KIS cash order business failure: {safe_code}")
    try:
        decoded = _AckEnvelope.model_validate(raw)
        safe_code = _safe_message_code(decoded.msg_cd)
    except (ValidationError, ValueError) as error:
        error.__traceback__ = None
        raise ValueError("KIS cash order success response is malformed") from None
    return KisCashOrderAck(
        organization_number=decoded.output.organization_number,
        order_number=decoded.output.order_number,
        order_time=decoded.output.order_time,
        message_code=safe_code,
    )


def build_cash_cancel_request(
    order: ProviderOrderIdentity,
    account: KisCashAccount,
) -> BrokerRequest:
    if type(order) is not ProviderOrderIdentity:
        raise TypeError("order must be an exact ProviderOrderIdentity")
    if type(account) is not KisCashAccount:
        raise TypeError("account must be an exact KisCashAccount")
    order.__post_init__()
    account.__post_init__()
    tr_id = (
        "TTTC0803U" if account.environment is KisCashEnvironment.LIVE else "VTTC0803U"
    )
    return _post_request(
        path=_CANCEL_PATH,
        tr_id=tr_id,
        body={
            "CANO": account.account_number,
            "ACNT_PRDT_CD": account.product_code,
            "KRX_FWDG_ORD_ORGNO": order.organization_number,
            "ORGN_ODNO": order.order_number,
            "ORD_DVSN": "00" if order.order_style is OrderStyle.LIMIT else "01",
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": decimal_to_string(order.remaining_quantity),
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
            "EXCG_ID_DVSN_CD": "KRX",
        },
    )


def _post_request(*, path: str, tr_id: str, body: dict[str, str]) -> BrokerRequest:
    return BrokerRequest(
        method="POST",
        path=path,
        headers=(
            ("content-type", "application/json; charset=utf-8"),
            ("custtype", "P"),
            ("tr_id", tr_id),
        ),
        body=json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def _order_tr_id(environment: KisCashEnvironment, side: Side) -> str:
    prefix = "TTTC" if environment is KisCashEnvironment.LIVE else "VTTC"
    return f"{prefix}0802U" if side is Side.BUY else f"{prefix}0801U"


def _positive_integer(value: object, name: str) -> Decimal:
    result = require_decimal(value)
    if result <= 0 or result != result.to_integral_value():
        raise ValueError(f"{name} must be a positive integer")
    return result


def _digits(value: object, length: int) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and value.isascii()
        and value.isdecimal()
    )


def _safe_message_code(value: object) -> str:
    if type(value) is not str or _SAFE_CODE.fullmatch(value) is None:
        raise ValueError("KIS message code is invalid")
    return value


def _require_uuid7(value: object, name: str) -> UUID:
    if not isinstance(value, UUID) or value.version != 7:
        raise ValueError(f"{name} must be UUIDv7")
    return value


__all__ = (
    "KisCashAccount",
    "KisCashEnvironment",
    "KisCashOrderAck",
    "KisCashOrderBusinessError",
    "LockedOrderIntent",
    "ProviderOrderIdentity",
    "build_cash_cancel_request",
    "build_cash_order_request",
    "decode_cash_order_response",
)
