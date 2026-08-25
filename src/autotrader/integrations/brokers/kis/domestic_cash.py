from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import NoReturn, cast
from urllib.parse import urlencode

from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)
from autotrader.integrations.brokers.kis.read_contracts import KisReadCredentials

_INVALID_RESPONSE = "KIS domestic cash response is invalid"
_INCOMPLETE = "KIS domestic cash snapshot is incomplete"
_REPEATED_CURSOR = "KIS domestic cash snapshot has a repeated continuation"
_BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
_BUYING_POWER_PATH = "/uapi/domestic-stock/v1/trading/inquire-psbl-order"


class KisIncompleteDomesticCashSnapshot(RuntimeError):
    """Raised when a complete KIS domestic-cash snapshot cannot be read."""


@dataclass(frozen=True, slots=True)
class KisDomesticCashAccount:
    account_number: str
    product_code: str

    def __post_init__(self) -> None:
        if not _digits(self.account_number, length=8) or not _digits(
            self.product_code, length=2
        ):
            raise ValueError("KIS domestic cash account is invalid")


@dataclass(frozen=True, slots=True)
class KisDomesticCashHolding:
    symbol: str
    quantity: Decimal

    def __post_init__(self) -> None:
        if not _digits(self.symbol, length=6) or not _valid_amount(
            self.quantity, positive=True
        ):
            raise ValueError("KIS domestic cash holding is invalid")


@dataclass(frozen=True, slots=True)
class KisDomesticCashBalanceSnapshot:
    holdings: tuple[KisDomesticCashHolding, ...]

    def __post_init__(self) -> None:
        if not _valid_holdings(self.holdings):
            raise ValueError("KIS domestic cash holdings must be an immutable tuple")


def _valid_holdings(value: object) -> bool:
    if not isinstance(value, tuple):
        return False
    holdings = cast(tuple[object, ...], value)
    return all(type(holding) is KisDomesticCashHolding for holding in holdings)


@dataclass(frozen=True, slots=True)
class KisDomesticCashBuyingPower:
    amount: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        if not _valid_amount(self.amount) or not _valid_amount(self.quantity):
            raise ValueError("KIS domestic cash buying power is invalid")


class KisDomesticCashReadOnlyAdapter:
    """Builds authenticated KIS domestic-cash observations, never writes."""

    _balance_path = _BALANCE_PATH
    _buying_power_path = _BUYING_POWER_PATH

    def __init__(self, *, transport: AsyncHttpTransport) -> None:
        self._transport = transport

    async def read_complete_balance(
        self,
        *,
        credentials: KisReadCredentials,
        account: KisDomesticCashAccount,
        max_pages: int,
    ) -> KisDomesticCashBalanceSnapshot:
        transport = self._transport
        error_message, snapshot = await _read_complete_balance(
            transport, credentials, account, max_pages
        )
        del self, transport, credentials, account, max_pages
        if error_message is not None:
            raise KisIncompleteDomesticCashSnapshot(error_message)
        return snapshot

    async def read_buying_power(
        self,
        *,
        credentials: KisReadCredentials,
        account: KisDomesticCashAccount,
        symbol: str,
        reference_price: Decimal,
    ) -> KisDomesticCashBuyingPower:
        transport = self._transport
        error_message, snapshot = await _read_buying_power(
            transport, credentials, account, symbol, reference_price
        )
        del self, transport, credentials, account, symbol, reference_price
        if error_message is not None:
            raise ValueError(error_message)
        return snapshot

    async def submit(self, *, command: object) -> NoReturn:
        del command
        raise BrokerWriteDisabled("KIS domestic cash write adapter is not enabled")

    async def cancel(self, *, command: object) -> NoReturn:
        del command
        raise BrokerWriteDisabled("KIS domestic cash write adapter is not enabled")

    async def replace(self, *, command: object) -> NoReturn:
        del command
        raise BrokerWriteDisabled("KIS domestic cash write adapter is not enabled")


def decode_kis_domestic_cash_balance_page(
    response: BrokerResponse,
) -> KisDomesticCashBalanceSnapshot:
    status = response.status
    body = response.body
    del response
    try:
        error_message, holdings, _ = _decode_balance_payload(status, body)
    finally:
        del body
    if error_message is not None:
        raise ValueError(error_message)
    return KisDomesticCashBalanceSnapshot(holdings=holdings)


def decode_kis_domestic_cash_buying_power(
    response: BrokerResponse,
) -> KisDomesticCashBuyingPower:
    status = response.status
    body = response.body
    del response
    try:
        error_message, snapshot = _decode_buying_power_payload(status, body)
    finally:
        del body
    if error_message is not None:
        raise ValueError(error_message)
    return snapshot


def _balance_request(
    credentials: KisReadCredentials,
    account: KisDomesticCashAccount,
    cursor: tuple[str, str] | None,
) -> BrokerRequest:
    fk100, nk100 = ("", "") if cursor is None else cursor
    query = urlencode(
        (
            ("CANO", account.account_number),
            ("ACNT_PRDT_CD", account.product_code),
            ("AFHR_FLPR_YN", "N"),
            ("OFL_YN", ""),
            ("INQR_DVSN", "02"),
            ("UNPR_DVSN", "01"),
            ("FUND_STTL_ICLD_YN", "N"),
            ("FNCG_AMT_AUTO_RDPT_YN", "N"),
            ("PRCS_DVSN", "00"),
            ("CTX_AREA_FK100", fk100),
            ("CTX_AREA_NK100", nk100),
        )
    )
    headers = _headers(credentials, "TTTC8434R")
    if cursor is not None:
        headers += (("tr_cont", "N"),)
    return BrokerRequest(
        method="GET",
        path=f"{_BALANCE_PATH}?{query}",
        headers=headers,
    )


def _buying_power_request(
    credentials: KisReadCredentials,
    account: KisDomesticCashAccount,
    symbol: str,
    reference_price: Decimal,
) -> BrokerRequest:
    query = urlencode(
        (
            ("CANO", account.account_number),
            ("ACNT_PRDT_CD", account.product_code),
            ("PDNO", symbol),
            ("ORD_UNPR", _integral_decimal_text(reference_price)),
            ("ORD_DVSN", "01"),
            ("CMA_EVLU_AMT_ICLD_YN", "N"),
            ("OVRS_ICLD_YN", "N"),
        )
    )
    return BrokerRequest(
        method="GET",
        path=f"{_BUYING_POWER_PATH}?{query}",
        headers=_headers(credentials, "TTTC8908R"),
    )


def _headers(
    credentials: KisReadCredentials, tr_id: str
) -> tuple[tuple[str, str], ...]:
    return (
        ("authorization", f"Bearer {credentials.access_token}"),
        ("appkey", credentials.app_key),
        ("appsecret", credentials.app_secret),
        ("tr_id", tr_id),
        ("custtype", "P"),
    )


async def _read_complete_balance(
    transport: AsyncHttpTransport,
    credentials: KisReadCredentials,
    account: object,
    max_pages: object,
) -> tuple[str | None, KisDomesticCashBalanceSnapshot]:
    fallback = KisDomesticCashBalanceSnapshot(holdings=())
    if type(max_pages) is not int or max_pages < 1:
        return _INCOMPLETE, fallback
    try:
        normalized_account = _account(account)
    except ValueError:
        return _INCOMPLETE, fallback
    holdings: list[KisDomesticCashHolding] = []
    cursor: tuple[str, str] | None = None
    seen_cursors: set[tuple[str, str]] = set()
    for _ in range(max_pages):
        response = await transport.request(
            _balance_request(credentials, normalized_account, cursor)
        )
        error_message, page_holdings, next_cursor = _decode_balance_response(response)
        continuation = response.header("tr_cont")
        del response
        if error_message is not None:
            return _INCOMPLETE, fallback
        holdings.extend(page_holdings)
        if continuation != "M":
            return None, KisDomesticCashBalanceSnapshot(holdings=tuple(holdings))
        if next_cursor is None:
            return _INCOMPLETE, fallback
        if next_cursor in seen_cursors:
            return _REPEATED_CURSOR, fallback
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return _INCOMPLETE, fallback


async def _read_buying_power(
    transport: AsyncHttpTransport,
    credentials: KisReadCredentials,
    account: object,
    symbol: object,
    reference_price: object,
) -> tuple[str | None, KisDomesticCashBuyingPower]:
    fallback = KisDomesticCashBuyingPower(amount=Decimal(), quantity=Decimal())
    try:
        normalized_account = _account(account)
        normalized_symbol = _symbol(symbol)
        if not _valid_amount(reference_price, positive=True):
            return "KIS domestic cash reference price is invalid", fallback
        response = await transport.request(
            _buying_power_request(
                credentials,
                normalized_account,
                normalized_symbol,
                cast(Decimal, reference_price),
            )
        )
        error_message, snapshot = _decode_buying_power_response(response)
        return error_message, snapshot
    except ValueError:
        return "KIS domestic cash request is invalid", fallback


def _integral_decimal_text(value: Decimal) -> str:
    return format(value.to_integral_value(), "f")


def _decode_balance_response(
    response: BrokerResponse,
) -> tuple[str | None, tuple[KisDomesticCashHolding, ...], tuple[str, str] | None]:
    status = response.status
    body = response.body
    del response
    try:
        return _decode_balance_payload(status, body)
    finally:
        del body


def _decode_buying_power_response(
    response: BrokerResponse,
) -> tuple[str | None, KisDomesticCashBuyingPower]:
    status = response.status
    body = response.body
    del response
    try:
        return _decode_buying_power_payload(status, body)
    finally:
        del body


def _decode_balance_payload(
    status: int, body: bytes
) -> tuple[str | None, tuple[KisDomesticCashHolding, ...], tuple[str, str] | None]:
    if status != 200:
        return _INVALID_RESPONSE, (), None
    try:
        payload: object = json.loads(body)
        if not isinstance(payload, dict):
            return _INVALID_RESPONSE, (), None
        typed_payload = cast(dict[str, object], payload)
        if typed_payload.get("rt_cd") != "0":
            return _INVALID_RESPONSE, (), None
        output = typed_payload.get("output1")
        if not isinstance(output, list):
            return _INVALID_RESPONSE, (), None
        holdings = tuple(_holding(item) for item in cast(list[object], output))
        fk100 = typed_payload.get("ctx_area_fk100")
        nk100 = typed_payload.get("ctx_area_nk100")
        cursor = (
            (fk100, nk100)
            if isinstance(fk100, str) and isinstance(nk100, str) and fk100 and nk100
            else None
        )
        return None, holdings, cursor
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        InvalidOperation,
        TypeError,
        ValueError,
    ):
        return _INVALID_RESPONSE, (), None


def _decode_buying_power_payload(
    status: int, body: bytes
) -> tuple[str | None, KisDomesticCashBuyingPower]:
    fallback = KisDomesticCashBuyingPower(amount=Decimal(), quantity=Decimal())
    if status != 200:
        return _INVALID_RESPONSE, fallback
    try:
        payload: object = json.loads(body)
        if not isinstance(payload, dict):
            return _INVALID_RESPONSE, fallback
        typed_payload = cast(dict[str, object], payload)
        if typed_payload.get("rt_cd") != "0":
            return _INVALID_RESPONSE, fallback
        output = typed_payload.get("output")
        if not isinstance(output, Mapping):
            return _INVALID_RESPONSE, fallback
        typed_output = cast(Mapping[str, object], output)
        return None, KisDomesticCashBuyingPower(
            amount=_provider_amount(typed_output.get("nrcvb_buy_amt")),
            quantity=_provider_amount(typed_output.get("nrcvb_buy_qty")),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        InvalidOperation,
        TypeError,
        ValueError,
    ):
        return _INVALID_RESPONSE, fallback


def _holding(value: object) -> KisDomesticCashHolding:
    if not isinstance(value, Mapping):
        raise ValueError
    payload = cast(Mapping[str, object], value)
    return KisDomesticCashHolding(
        symbol=_symbol(payload.get("pdno")),
        quantity=_provider_amount(payload.get("hldg_qty")),
    )


def _provider_amount(value: object) -> Decimal:
    if not isinstance(value, str) or not _digits(value) or len(value) > 30:
        raise ValueError
    return Decimal(value)


def _account(value: object) -> KisDomesticCashAccount:
    if not isinstance(value, KisDomesticCashAccount):
        raise ValueError("KIS domestic cash account is invalid")
    return value


def _symbol(value: object) -> str:
    if not _digits(value, length=6):
        raise ValueError("KIS domestic cash symbol is invalid")
    return cast(str, value)


def _digits(value: object, *, length: int | None = None) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and (length is None or len(value) == length)
        and value.isascii()
        and value.isdecimal()
    )


def _valid_amount(value: object, *, positive: bool = False) -> bool:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        return False
    if (positive and value == 0) or value != value.to_integral_value():
        return False
    return value == 0 or value.adjusted() + 1 <= 30
