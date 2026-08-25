from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import cast
from urllib.parse import urlencode

from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerRequest,
    BrokerResponse,
)
from autotrader.integrations.brokers.kis.account_snapshot_contracts import (
    KisDomesticCashEnvironment,
    KisKrDomesticCashPosition,
    KisStableKrDomesticCashAccountSnapshot,
)
from autotrader.integrations.brokers.kis.domestic_cash import KisDomesticCashAccount
from autotrader.integrations.brokers.kis.read_contracts import KisReadCredentials

_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
_INCOMPLETE = "KIS account snapshot is incomplete"


class KisIncompleteAccountSnapshot(RuntimeError):
    """Raised when a complete stable KIS cash-account projection is unavailable."""


_TR_IDS = {
    KisDomesticCashEnvironment.REAL: "TTTC8434R",
    KisDomesticCashEnvironment.PAPER: "VTTC8434R",
}


@dataclass(frozen=True, slots=True)
class _KisAccountCapture:
    total_deposit_cash: Decimal
    positions: tuple[KisKrDomesticCashPosition, ...]


@dataclass(frozen=True, slots=True)
class _KisAccountPage:
    cash: Decimal
    rows: tuple[tuple[str, Decimal, Decimal], ...]
    cursor: tuple[str, str] | None
    continuation: str | None


async def collect_stable_kis_kr_domestic_cash_account_snapshot(
    *,
    transport: object,
    environment: object,
    credentials: object,
    account: object,
    max_pages: object,
    clock: Callable[[], datetime] | None = None,
) -> KisStableKrDomesticCashAccountSnapshot:
    first: _KisAccountCapture | None = None
    second: _KisAccountCapture | None = None
    observed_at: object = None
    snapshot: KisStableKrDomesticCashAccountSnapshot | None = None
    incomplete = False
    try:
        if (
            type(credentials) is not KisReadCredentials
            or type(account) is not KisDomesticCashAccount
            or account.product_code != "01"
            or type(environment) is not KisDomesticCashEnvironment
            or type(max_pages) is not int
            or max_pages <= 0
            or not hasattr(transport, "request")
        ):
            raise ValueError("KIS account snapshot input is invalid")
        credentials.__post_init__()
        account.__post_init__()
        observed_at = (_utc_now if clock is None else clock)()
        if (
            type(observed_at) is not datetime
            or observed_at.tzinfo is not UTC
            or observed_at.microsecond != 0
        ):
            raise ValueError("KIS account snapshot observed time is invalid")
        first = await _capture_complete_projection(
            transport=cast(AsyncHttpTransport, transport),
            environment=environment,
            credentials=credentials,
            account=account,
            max_pages=max_pages,
        )
        second = await _capture_complete_projection(
            transport=cast(AsyncHttpTransport, transport),
            environment=environment,
            credentials=credentials,
            account=account,
            max_pages=max_pages,
        )
        if first != second:
            raise ValueError("KIS account projection changed during collection")
        snapshot = KisStableKrDomesticCashAccountSnapshot.build(
            observed_at=observed_at,
            environment=environment,
            total_deposit_cash=first.total_deposit_cash,
            positions=first.positions,
        )
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as caught:
        _scrub_control(caught)
        del caught
        raise
    except Exception as caught:
        _scrub_exception(caught)
        del caught
        incomplete = True
    finally:
        first = None
        second = None
        observed_at = None
        del transport, environment, credentials, account, max_pages, clock
    if incomplete or snapshot is None:
        raise KisIncompleteAccountSnapshot(_INCOMPLETE) from None
    return snapshot


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


async def _capture_complete_projection(
    *,
    transport: AsyncHttpTransport,
    environment: KisDomesticCashEnvironment,
    credentials: KisReadCredentials,
    account: KisDomesticCashAccount,
    max_pages: int,
) -> _KisAccountCapture:
    cursor: tuple[str, str] | None = None
    seen_cursors: set[tuple[str, str]] = set()
    seen_symbols: set[str] = set()
    positions: list[KisKrDomesticCashPosition] = []
    cash: Decimal | None = None
    request: BrokerRequest | None = None
    response: BrokerResponse | None = None
    page: _KisAccountPage | None = None
    try:
        for _ in range(max_pages):
            request = _balance_request(environment, credentials, account, cursor)
            response = await transport.request(request)
            page = _decode_page(response)
            response = None
            request = None
            if cash is None:
                cash = page.cash
            elif cash != page.cash:
                raise ValueError("KIS account cash changed within one capture")
            for symbol, total, available in page.rows:
                if symbol in seen_symbols:
                    raise ValueError("KIS account projection has a duplicate product")
                seen_symbols.add(symbol)
                if total > 0:
                    positions.append(
                        KisKrDomesticCashPosition(
                            symbol=symbol,
                            total_quantity=total,
                            order_available_quantity=available,
                        )
                    )
            if page.continuation is None:
                return _KisAccountCapture(
                    total_deposit_cash=cash,
                    positions=tuple(sorted(positions, key=lambda item: item.symbol)),
                )
            if page.cursor is None or page.cursor in seen_cursors:
                raise ValueError("KIS account continuation is invalid")
            seen_cursors.add(page.cursor)
            cursor = page.cursor
            page = None
        raise ValueError("KIS account page bound was exhausted")
    finally:
        request = None
        response = None
        page = None
        positions.clear()
        seen_cursors.clear()
        seen_symbols.clear()
        del transport, environment, credentials, account


def _balance_request(
    environment: KisDomesticCashEnvironment,
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
    headers = (
        ("authorization", f"Bearer {credentials.access_token}"),
        ("appkey", credentials.app_key),
        ("appsecret", credentials.app_secret),
        ("tr_id", _TR_IDS[environment]),
        ("custtype", "P"),
    )
    if cursor is not None:
        headers += (("tr_cont", "N"),)
    return BrokerRequest(method="GET", path=f"{_PATH}?{query}", headers=headers)


def _decode_page(response: BrokerResponse) -> _KisAccountPage:
    status = response.status
    body = response.body
    continuation = response.header("tr_cont")
    del response
    try:
        page = _decode_page_values(status, body, continuation)
    finally:
        del body
    if page is None:
        raise ValueError("KIS account response is invalid") from None
    return page


def _decode_page_values(
    status: int, body: bytes, continuation: str | None
) -> _KisAccountPage | None:
    if status != 200:
        return None
    try:
        payload: object = json.loads(body)
        if not isinstance(payload, dict):
            return None
        typed = cast(dict[str, object], payload)
        if typed.get("rt_cd") != "0":
            return None
        output1 = typed.get("output1")
        output2 = typed.get("output2")
        if not isinstance(output1, list) or not isinstance(output2, list):
            return None
        typed_output2 = cast(list[object], output2)
        if len(typed_output2) != 1:
            return None
        summary = typed_output2[0]
        if not isinstance(summary, Mapping):
            return None
        rows = tuple(_row(item) for item in cast(list[object], output1))
        cash = _provider_amount(cast(Mapping[str, object], summary).get("dnca_tot_amt"))
        cursor = _cursor(typed)
        if continuation in (None, "", "D", "E"):
            normalized_continuation = None
        elif continuation in ("M", "F"):
            normalized_continuation = continuation
        else:
            return None
        if normalized_continuation is not None and cursor is None:
            return None
        return _KisAccountPage(
            cash=cash,
            rows=rows,
            cursor=cursor,
            continuation=normalized_continuation,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        InvalidOperation,
        TypeError,
        ValueError,
    ):
        return None


def _row(value: object) -> tuple[str, Decimal, Decimal]:
    if not isinstance(value, Mapping):
        raise ValueError
    row = cast(Mapping[str, object], value)
    symbol = row.get("pdno")
    if not _digits(symbol, length=6):
        raise ValueError
    total = _provider_amount(row.get("hldg_qty"))
    available = _provider_amount(row.get("ord_psbl_qty"))
    if available > total:
        raise ValueError
    return cast(str, symbol), total, available


def _cursor(payload: Mapping[str, object]) -> tuple[str, str] | None:
    fk100 = payload.get("ctx_area_fk100")
    nk100 = payload.get("ctx_area_nk100")
    if fk100 == "" and nk100 == "":
        return None
    if (
        isinstance(fk100, str)
        and isinstance(nk100, str)
        and fk100
        and nk100
        and "\n" not in fk100
        and "\n" not in nk100
    ):
        return fk100, nk100
    raise ValueError


def _provider_amount(value: object) -> Decimal:
    if not _digits(value) or len(cast(str, value)) > 30:
        raise ValueError
    return Decimal(cast(str, value))


def _digits(value: object, *, length: int | None = None) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and (length is None or len(value) == length)
        and value.isascii()
        and value.isdecimal()
    )


def _scrub_control(
    caught: asyncio.CancelledError | KeyboardInterrupt | SystemExit,
) -> None:
    caught.__traceback__ = None
    caught.__context__ = None
    caught.__cause__ = None
    caught.args = ()
    caught.__dict__.clear()
    if isinstance(caught, SystemExit):
        caught.code = 1


def _scrub_exception(caught: Exception) -> None:
    caught.__traceback__ = None
    caught.__context__ = None
    caught.__cause__ = None
    caught.args = ()
    caught.__dict__.clear()
