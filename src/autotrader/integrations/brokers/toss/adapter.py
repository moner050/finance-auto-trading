from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import NoReturn, cast
from urllib.parse import quote, urlencode

from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerMarket,
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
    UnsupportedBrokerInstrument,
    UnsupportedBrokerMarket,
)
from autotrader.integrations.brokers.toss import (
    stock_order_contracts as _stock_order_contracts,
)
from autotrader.integrations.brokers.toss.market_data_contracts import (
    TossCandleInterval,
    TossCandlePage,
    TossCandleRecord,
)
from autotrader.integrations.brokers.toss.market_data_reader import (
    TossAccessToken,
    TossClientCredentials,
)
from autotrader.integrations.brokers.toss.stock_order_contracts import (
    TossOrderSubmissionAcknowledgement as TossOrderSubmissionAcknowledgement,
)
from autotrader.integrations.brokers.toss.stock_order_contracts import (
    TossStockOrderPreview as TossStockOrderPreview,
)
from autotrader.integrations.brokers.toss.stock_order_contracts import (
    TossStockOrderPreviewError as TossStockOrderPreviewError,
)
from autotrader.integrations.brokers.toss.stock_order_contracts import (
    build_toss_stock_order_preview as build_toss_stock_order_preview,
)
from autotrader.shared.decimal import (
    decimal_to_string,
    parse_contract_decimal,
    require_decimal,
)

decode_toss_order_submission_acknowledgement = (
    _stock_order_contracts.decode_toss_order_submission_acknowledgement
)


class TossAuthenticationError(RuntimeError):
    """Raised when Toss does not return a valid OAuth access token."""


class TossIncompleteAccountSnapshot(RuntimeError):
    """Raised when a paginated Toss account observation is incomplete."""


class TossIncompleteCandleSnapshot(RuntimeError):
    """Raised when Toss candle pages cannot be collected completely."""


@dataclass(frozen=True, slots=True)
class TossAccount:
    """An in-memory Toss account scope that deliberately omits accountNo."""

    account_seq: int
    account_type: str

    def __post_init__(self) -> None:
        _account_sequence(self.account_seq)
        if not self.account_type or "\n" in self.account_type:
            raise ValueError("Toss account type is invalid")


@dataclass(frozen=True, slots=True)
class TossKrwCashBuyingPower:
    """An in-memory KRW cash-only buying-power observation."""

    amount: Decimal

    def __post_init__(self) -> None:
        amount = require_decimal(self.amount)
        if (
            amount < 0
            or amount != amount.to_integral_value()
            or len(decimal_to_string(amount)) > 30
        ):
            raise ValueError("Toss KRW cash buying power is invalid")
        object.__setattr__(self, "amount", amount)


@dataclass(frozen=True, slots=True)
class TossPriceRecord:
    """One provider current-price record; it is not a completed strategy bar."""

    symbol: str
    timestamp: datetime | None
    last_price: Decimal
    currency: str

    def __post_init__(self) -> None:
        _stock_symbol(self.symbol)
        if self.timestamp is not None and (
            type(self.timestamp) is not datetime
            or self.timestamp.tzinfo is None
            or self.timestamp.utcoffset() is None
        ):
            raise ValueError("Toss price timestamp must be timezone-aware")
        last_price = require_decimal(self.last_price)
        if last_price <= 0:
            raise ValueError("Toss price is invalid")
        object.__setattr__(self, "last_price", last_price)
        if not self.currency or "\n" in self.currency:
            raise ValueError("Toss price currency is invalid")


@dataclass(frozen=True, slots=True)
class TossPricePage:
    """A raw Toss current-price response page."""

    records: tuple[TossPriceRecord, ...]

    def __post_init__(self) -> None:
        records = cast(object, self.records)
        if not isinstance(records, tuple):
            raise ValueError("Toss price records must be an immutable tuple")
        records = cast(tuple[object, ...], records)
        if not all(isinstance(record, TossPriceRecord) for record in records):
            raise ValueError("Toss price records must be an immutable tuple")


class TossReadOnlyAdapter:
    """Constructs only authenticated Toss read requests through injected transport."""

    base_url = "https://openapi.tossinvest.com"

    def __init__(self, *, transport: AsyncHttpTransport) -> None:
        self._transport = transport

    async def issue_access_token(
        self, *, credentials: TossClientCredentials
    ) -> TossAccessToken:
        response = await self._transport.request(
            BrokerRequest(
                method="POST",
                path="/oauth2/token",
                headers=(("Content-Type", "application/x-www-form-urlencoded"),),
                body=urlencode(
                    (
                        ("grant_type", "client_credentials"),
                        ("client_id", credentials.client_id),
                        ("client_secret", credentials.client_secret),
                    )
                ).encode("utf-8"),
            )
        )
        if response.status != 200:
            raise TossAuthenticationError("Toss OAuth token request failed")
        return _access_token_from_response(response.body)

    async def read_price(
        self, *, market: BrokerMarket, symbol: str, access_token: str
    ) -> BrokerResponse:
        if market not in {BrokerMarket.KRX_STOCK, BrokerMarket.US_STOCK}:
            raise UnsupportedBrokerMarket(f"Toss does not support {market}")
        normalized_symbol = _stock_symbol(symbol)
        return await self._transport.request(
            BrokerRequest(
                method="GET",
                path=f"/api/v1/prices?symbols={normalized_symbol}",
                headers=(("Authorization", f"Bearer {_token(access_token)}"),),
            )
        )

    async def read_orderbook(
        self, *, market: BrokerMarket, symbol: str, access_token: str
    ) -> BrokerResponse:
        if market not in {BrokerMarket.KRX_STOCK, BrokerMarket.US_STOCK}:
            raise UnsupportedBrokerMarket(f"Toss does not support {market}")
        return await self._transport.request(
            BrokerRequest(
                method="GET",
                path=f"/api/v1/orderbook?symbol={_stock_symbol(symbol)}",
                headers=(("Authorization", f"Bearer {_token(access_token)}"),),
            )
        )

    async def read_recent_trades(
        self,
        *,
        market: BrokerMarket,
        symbol: str,
        count: object,
        access_token: str,
    ) -> BrokerResponse:
        if market not in {BrokerMarket.KRX_STOCK, BrokerMarket.US_STOCK}:
            raise UnsupportedBrokerMarket(f"Toss does not support {market}")
        return await self._transport.request(
            BrokerRequest(
                method="GET",
                path=(
                    f"/api/v1/trades?symbol={_stock_symbol(symbol)}&"
                    f"count={_recent_trade_count(count)}"
                ),
                headers=(("Authorization", f"Bearer {_token(access_token)}"),),
            )
        )

    async def read_accounts(self, *, access_token: str) -> BrokerResponse:
        return await self._transport.request(
            BrokerRequest(
                method="GET",
                path="/api/v1/accounts",
                headers=(("Authorization", f"Bearer {_token(access_token)}"),),
            )
        )

    async def read_krw_cash_buying_power(
        self, *, access_token: str, account: TossAccount
    ) -> BrokerResponse:
        if not isinstance(cast(object, account), TossAccount):
            raise ValueError("Toss cash buying power requires a TossAccount")
        return await self._transport.request(
            BrokerRequest(
                method="GET",
                path="/api/v1/buying-power?currency=KRW",
                headers=_account_headers(
                    access_token=access_token,
                    account_seq=_account_sequence(account.account_seq),
                ),
            )
        )

    async def read_sellable_quantity(
        self, *, access_token: str, account_seq: object, symbol: str
    ) -> BrokerResponse:
        return await self._transport.request(
            BrokerRequest(
                method="GET",
                path=(
                    "/api/v1/sellable-quantity?"
                    f"{urlencode((('symbol', _stock_symbol(symbol)),))}"
                ),
                headers=_account_headers(
                    access_token=access_token,
                    account_seq=_account_sequence(account_seq),
                ),
            )
        )

    async def read_holdings(
        self, *, access_token: str, account_seq: object, symbol: str | None = None
    ) -> BrokerResponse:
        path = "/api/v1/holdings"
        if symbol is not None:
            path = f"{path}?{urlencode((('symbol', _stock_symbol(symbol)),))}"
        return await self._transport.request(
            BrokerRequest(
                method="GET",
                path=path,
                headers=_account_headers(
                    access_token=access_token,
                    account_seq=_account_sequence(account_seq),
                ),
            )
        )

    async def read_orders(
        self,
        *,
        access_token: str,
        account_seq: object,
        status: object,
        symbol: str | None = None,
    ) -> BrokerResponse:
        query = [("status", _order_status(status))]
        if symbol is not None:
            query.append(("symbol", _stock_symbol(symbol)))
        return await self._transport.request(
            BrokerRequest(
                method="GET",
                path=f"/api/v1/orders?{urlencode(query)}",
                headers=_account_headers(
                    access_token=access_token,
                    account_seq=_account_sequence(account_seq),
                ),
            )
        )

    async def read_order_detail(
        self,
        *,
        access_token: str,
        account_seq: object,
        order_id: str,
    ) -> BrokerResponse:
        return await self._transport.request(
            BrokerRequest(
                method="GET",
                path=f"/api/v1/orders/{_opaque_order_id(order_id)}",
                headers=_account_headers(
                    access_token=access_token,
                    account_seq=_account_sequence(account_seq),
                ),
            )
        )

    async def read_complete_closed_orders(
        self,
        *,
        access_token: str,
        account_seq: object,
        start_date: date | None,
        end_date: date | None,
        symbol: str | None,
        max_pages: object,
    ) -> tuple[BrokerResponse, ...]:
        normalized_start, normalized_end = _order_history_date_range(
            start_date=start_date,
            end_date=end_date,
        )
        normalized_account = _account_sequence(account_seq)
        normalized_symbol = None if symbol is None else _stock_symbol(symbol)
        page_limit = _order_history_page_limit(max_pages)
        cursor: str | None = None
        pages: list[BrokerResponse] = []
        for _ in range(page_limit):
            query = [("status", "CLOSED")]
            if normalized_symbol is not None:
                query.append(("symbol", normalized_symbol))
            if normalized_start is not None:
                query.append(("from", normalized_start.isoformat()))
            if normalized_end is not None:
                query.append(("to", normalized_end.isoformat()))
            query.append(("limit", "100"))
            if cursor is not None:
                query.append(("cursor", cursor))
            response = await self._transport.request(
                BrokerRequest(
                    method="GET",
                    path=f"/api/v1/orders?{urlencode(query)}",
                    headers=_account_headers(
                        access_token=access_token,
                        account_seq=normalized_account,
                    ),
                )
            )
            has_next, cursor = _closed_order_page_continuation(response)
            pages.append(response)
            if not has_next:
                return tuple(pages)
        raise TossIncompleteAccountSnapshot(
            "Toss closed order history exceeded the declared page limit"
        )

    async def read_candles(
        self,
        *,
        market: BrokerMarket,
        symbol: str,
        interval: object,
        count: object,
        before: datetime | str | None,
        adjusted: object,
        access_token: str,
    ) -> BrokerResponse:
        if market not in {BrokerMarket.KRX_STOCK, BrokerMarket.US_STOCK}:
            raise UnsupportedBrokerMarket(f"Toss does not support {market}")
        normalized_interval = _candle_interval(interval)
        normalized_count = _candle_count(count)
        normalized_before = _candle_before(before)
        normalized_adjusted = _adjusted(adjusted)
        query = [
            ("symbol", _stock_symbol(symbol)),
            ("interval", normalized_interval.value),
            ("count", str(normalized_count)),
        ]
        if normalized_before is not None:
            query.append(("before", normalized_before))
        query.append(("adjusted", str(normalized_adjusted).lower()))
        return await self._transport.request(
            BrokerRequest(
                method="GET",
                path=f"/api/v1/candles?{urlencode(query)}",
                headers=(("Authorization", f"Bearer {_token(access_token)}"),),
            )
        )

    async def read_complete_candle_pages(
        self,
        *,
        market: BrokerMarket,
        symbol: str,
        interval: TossCandleInterval,
        count: int,
        before: datetime | str | None,
        adjusted: bool,
        access_token: str,
        max_pages: object,
    ) -> tuple[TossCandlePage, ...]:
        return await self._read_candle_pages(
            market=market,
            symbol=symbol,
            interval=interval,
            count=count,
            before=before,
            adjusted=adjusted,
            access_token=access_token,
            max_pages=max_pages,
            require_terminal_page=True,
        )

    async def read_recent_candle_pages(
        self,
        *,
        market: BrokerMarket,
        symbol: str,
        interval: TossCandleInterval,
        count: int,
        before: datetime | str | None,
        adjusted: bool,
        access_token: str,
        max_pages: object,
    ) -> tuple[TossCandlePage, ...]:
        return await self._read_candle_pages(
            market=market,
            symbol=symbol,
            interval=interval,
            count=count,
            before=before,
            adjusted=adjusted,
            access_token=access_token,
            max_pages=max_pages,
            require_terminal_page=False,
        )

    async def _read_candle_pages(
        self,
        *,
        market: BrokerMarket,
        symbol: str,
        interval: TossCandleInterval,
        count: int,
        before: datetime | str | None,
        adjusted: bool,
        access_token: str,
        max_pages: object,
        require_terminal_page: bool,
    ) -> tuple[TossCandlePage, ...]:
        page_limit = _candle_page_limit(max_pages)
        cursor = _candle_before(before)
        cursor_at = (
            None if cursor is None else _provider_datetime(cursor, name="before")
        )
        pages: list[TossCandlePage] = []
        for _ in range(page_limit):
            response = await self.read_candles(
                market=market,
                symbol=symbol,
                interval=interval,
                count=count,
                before=cursor,
                adjusted=adjusted,
                access_token=access_token,
            )
            page = decode_toss_candle_page(response)
            pages.append(page)
            if page.next_before is None:
                return tuple(pages)
            next_at = _provider_datetime(page.next_before, name="nextBefore")
            if not page.records or (cursor_at is not None and next_at >= cursor_at):
                raise TossIncompleteCandleSnapshot("Toss candle cursor did not advance")
            cursor, cursor_at = page.next_before, next_at
        if require_terminal_page:
            raise TossIncompleteCandleSnapshot(
                "Toss candle pages exceeded the declared limit"
            )
        return tuple(pages)

    async def submit(self, *, command: object) -> NoReturn:
        del command
        raise BrokerWriteDisabled("Toss write adapter is not enabled")

    async def cancel(self, *, command: object) -> NoReturn:
        del command
        raise BrokerWriteDisabled("Toss write adapter is not enabled")

    async def replace(self, *, command: object) -> NoReturn:
        del command
        raise BrokerWriteDisabled("Toss write adapter is not enabled")


def _token(value: str) -> str:
    if not value or "\n" in value:
        raise ValueError("access token must be a non-empty single line")
    return value


def _account_sequence(value: object) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("account sequence must be a positive integer")
    return str(value)


def _account_headers(
    *, access_token: str, account_seq: str
) -> tuple[tuple[str, str], ...]:
    return (
        ("Authorization", f"Bearer {_token(access_token)}"),
        ("X-Tossinvest-Account", account_seq),
    )


def _order_status(value: object) -> str:
    if value not in {"OPEN", "CLOSED"}:
        raise ValueError("Toss order status must be OPEN or CLOSED")
    return cast(str, value)


def _opaque_order_id(value: str) -> str:
    if not value or "\n" in value:
        raise ValueError("Toss order id must be a non-empty single line")
    return quote(value, safe="")


def _order_history_date_range(
    *, start_date: date | None, end_date: date | None
) -> tuple[date | None, date | None]:
    if (
        (start_date is not None and type(start_date) is not date)
        or (end_date is not None and type(end_date) is not date)
        or (start_date is not None and end_date is not None and start_date > end_date)
    ):
        raise ValueError("Toss closed order history requires a valid date range")
    return start_date, end_date


def _order_history_page_limit(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 10:
        raise ValueError(
            "Toss closed order history page limit must be an integer from 1 through 10"
        )
    return value


def _closed_order_page_continuation(
    response: BrokerResponse,
) -> tuple[bool, str | None]:
    if response.status != 200:
        raise TossIncompleteAccountSnapshot(
            "Toss closed order history page was not successful"
        )
    try:
        payload: object = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TossIncompleteAccountSnapshot(
            "Toss closed order history response is not valid JSON"
        ) from error
    if not isinstance(payload, Mapping):
        raise TossIncompleteAccountSnapshot(
            "Toss closed order history response is not an object"
        )
    payload = cast(Mapping[str, object], payload)
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise TossIncompleteAccountSnapshot(
            "Toss closed order history response is incomplete"
        )
    result = cast(Mapping[str, object], result)
    if not isinstance(result.get("orders"), list):
        raise TossIncompleteAccountSnapshot(
            "Toss closed order history response is incomplete"
        )
    has_next = result.get("hasNext")
    cursor = result.get("nextCursor")
    if not isinstance(has_next, bool):
        raise TossIncompleteAccountSnapshot(
            "Toss closed order history continuation is invalid"
        )
    if has_next:
        if not isinstance(cursor, str) or not cursor or "\n" in cursor:
            raise TossIncompleteAccountSnapshot(
                "Toss closed order history continuation is invalid"
            )
        return True, cursor
    if cursor is not None:
        raise TossIncompleteAccountSnapshot(
            "Toss closed order history continuation is invalid"
        )
    return False, None


def _stock_symbol(value: str) -> str:
    symbol = value.upper()
    if symbol in {"NQ", "MNQ"}:
        raise UnsupportedBrokerInstrument(f"Toss does not support {symbol}")
    if not symbol or any(
        not (character.isalnum() or character in ".-") for character in symbol
    ):
        raise UnsupportedBrokerInstrument("Toss stock symbol is invalid")
    return symbol


def _recent_trade_count(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 50:
        raise ValueError("Toss recent trade count must be an integer from 1 through 50")
    return value


def _candle_interval(value: object) -> TossCandleInterval:
    if not isinstance(value, TossCandleInterval):
        raise ValueError("candle interval must be a TossCandleInterval")
    return value


def _candle_count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 200:
        raise ValueError("candle count must be an integer from 1 through 200")
    return value


def _candle_page_limit(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 10:
        raise ValueError("candle page limit must be an integer from 1 through 10")
    return value


def _candle_before(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        _provider_datetime(value, name="before")
        return value
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("candle cursor must be an aware datetime")
    return value.isoformat()


def _adjusted(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("adjusted must be a bool")
    return value


def _access_token_from_response(body: bytes) -> TossAccessToken:
    try:
        payload: object = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TossAuthenticationError(
            "Toss OAuth response is not valid JSON"
        ) from error
    if not isinstance(payload, dict):
        raise TossAuthenticationError("Toss OAuth response is not an object")
    payload = cast(dict[str, object], payload)
    access_token = payload.get("access_token")
    token_type = payload.get("token_type")
    expires_in = payload.get("expires_in")
    if (
        not isinstance(access_token, str)
        or token_type != "Bearer"
        or not isinstance(expires_in, int)
        or isinstance(expires_in, bool)
    ):
        raise TossAuthenticationError("Toss OAuth response is incomplete")
    try:
        return TossAccessToken(value=access_token, expires_in_seconds=expires_in)
    except ValueError as error:
        raise TossAuthenticationError("Toss OAuth response is invalid") from error


def decode_toss_candle_page(response: BrokerResponse) -> TossCandlePage:
    """Decodes raw Toss OHLCV fields without assigning strategy-bar semantics."""
    if response.status != 200:
        raise ValueError("Toss candle response is not successful")
    try:
        payload: object = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Toss candle response is not valid JSON") from error
    if not isinstance(payload, Mapping):
        raise ValueError("Toss candle response is not an object")
    payload = cast(Mapping[str, object], payload)
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("Toss candle response result is invalid")
    result = cast(Mapping[str, object], result)
    candles = result.get("candles")
    if not isinstance(candles, list):
        raise ValueError("Toss candle response candles are invalid")
    candles = cast(list[object], candles)
    next_before = result.get("nextBefore")
    if next_before is not None:
        if not isinstance(next_before, str):
            raise ValueError("Toss candle nextBefore is invalid")
        _provider_datetime(next_before, name="nextBefore")
    return TossCandlePage(
        records=tuple(_candle_record(candle) for candle in candles),
        next_before=next_before,
    )


def decode_toss_price_page(response: BrokerResponse) -> TossPricePage:
    """Decodes raw Toss current prices without assigning bar-completion semantics."""
    if response.status != 200:
        raise ValueError("Toss price response is not successful")
    try:
        payload: object = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Toss price response is not valid JSON") from error
    if not isinstance(payload, Mapping):
        raise ValueError("Toss price response is not an object")
    payload = cast(Mapping[str, object], payload)
    result = payload.get("result")
    if not isinstance(result, list):
        raise ValueError("Toss price response result is invalid")
    result = cast(list[object], result)
    return TossPricePage(records=tuple(_price_record(record) for record in result))


def decode_toss_accounts(response: BrokerResponse) -> tuple[TossAccount, ...]:
    status = response.status
    body = response.body
    del response
    try:
        error_message, accounts = _decode_toss_account_response(status, body)
    finally:
        del body
    if error_message is not None:
        raise ValueError(error_message)
    return accounts


def decode_toss_krw_cash_buying_power(
    response: BrokerResponse,
) -> TossKrwCashBuyingPower:
    status = response.status
    body = response.body
    del response
    try:
        error_message, snapshot = _decode_toss_krw_cash_buying_power_response(
            status, body
        )
    finally:
        del body
    if error_message is not None:
        raise ValueError(error_message)
    return snapshot


def decode_toss_krx_cash_holding_presence(
    response: BrokerResponse, *, symbol: str
) -> bool:
    status = response.status
    body = response.body
    del response
    try:
        try:
            normalized_symbol = _stock_symbol(symbol)
        except AttributeError, ValueError:
            error_message, present = (
                "Toss KRX cash holding symbol is invalid",
                False,
            )
        else:
            error_message, present = _decode_toss_krx_cash_holding_presence(
                status, body, normalized_symbol
            )
    finally:
        del body
    if error_message is not None:
        raise ValueError(error_message)
    return present


def _decode_toss_krx_cash_holding_presence(
    status: int, body: bytes, symbol: str
) -> tuple[str | None, bool]:
    error_message = "Toss KRX cash holding response is invalid"
    if status != 200:
        return error_message, False
    try:
        payload: object = json.loads(body)
    except RecursionError, ValueError:
        return error_message, False
    if not isinstance(payload, Mapping):
        return error_message, False
    result = cast(Mapping[str, object], payload).get("result")
    if not isinstance(result, Mapping):
        return error_message, False
    items = cast(Mapping[str, object], result).get("items")
    if not isinstance(items, list):
        return error_message, False
    raw_items = cast(list[object], items)
    if not raw_items:
        return None, False
    if len(raw_items) != 1 or not isinstance(raw_items[0], Mapping):
        return error_message, False
    item = cast(Mapping[str, object], raw_items[0])
    quantity = item.get("quantity")
    if (
        item.get("symbol") != symbol
        or item.get("marketCountry") != "KR"
        or item.get("currency") != "KRW"
        or not isinstance(quantity, str)
        or len(quantity) > 30
        or not quantity.isascii()
        or not quantity.isdecimal()
    ):
        return error_message, False
    try:
        quantity_decimal = parse_contract_decimal(quantity)
    except ValueError, ArithmeticError:
        return error_message, False
    if (
        quantity_decimal <= 0
        or quantity_decimal != quantity_decimal.to_integral_value()
    ):
        return error_message, False
    return None, True


def _decode_toss_krw_cash_buying_power_response(
    status: int, body: bytes
) -> tuple[str | None, TossKrwCashBuyingPower]:
    fallback = TossKrwCashBuyingPower(amount=Decimal())
    if status != 200:
        return "Toss KRW cash buying power response is invalid", fallback
    try:
        payload: object = json.loads(body)
    except UnicodeDecodeError, json.JSONDecodeError, RecursionError:
        return "Toss KRW cash buying power response is invalid", fallback
    if not isinstance(payload, Mapping):
        return "Toss KRW cash buying power response is invalid", fallback
    result = cast(Mapping[str, object], payload).get("result")
    if not isinstance(result, Mapping):
        return "Toss KRW cash buying power response is invalid", fallback
    result = cast(Mapping[str, object], result)
    currency = result.get("currency")
    amount = result.get("cashBuyingPower")
    if (
        currency != "KRW"
        or not isinstance(amount, str)
        or len(amount) > 30
        or not amount.isascii()
        or not amount.isdecimal()
    ):
        return "Toss KRW cash buying power response is invalid", fallback
    try:
        amount = parse_contract_decimal(amount)
    except ValueError:
        return "Toss KRW cash buying power response is invalid", fallback
    if amount < 0 or amount != amount.to_integral_value():
        return "Toss KRW cash buying power response is invalid", fallback
    return None, TossKrwCashBuyingPower(amount=amount)


def _decode_toss_account_response(
    status: int, body: bytes
) -> tuple[str | None, tuple[TossAccount, ...]]:
    if status != 200:
        return "Toss account response is not successful", ()
    try:
        payload: object = json.loads(body)
    except UnicodeDecodeError, json.JSONDecodeError:
        return "Toss account response is not valid JSON", ()
    if not isinstance(payload, Mapping):
        return "Toss account response is not an object", ()
    result = cast(Mapping[str, object], payload).get("result")
    if not isinstance(result, list):
        return "Toss account response result is invalid", ()
    try:
        return None, tuple(_toss_account(item) for item in cast(list[object], result))
    except ValueError as error:
        return str(error), ()


def select_single_brokerage_account(accounts: object) -> TossAccount:
    if not isinstance(accounts, tuple):
        raise ValueError("Toss accounts must be an immutable tuple")
    tuple_accounts = cast(tuple[object, ...], accounts)
    if not all(isinstance(account, TossAccount) for account in tuple_accounts):
        raise ValueError("Toss accounts must be an immutable tuple")
    typed_accounts = cast(tuple[TossAccount, ...], tuple_accounts)
    matches = tuple(
        account for account in typed_accounts if account.account_type == "BROKERAGE"
    )
    if len(matches) != 1 or len(typed_accounts) != 1:
        raise ValueError("Toss requires exactly one brokerage account")
    return matches[0]


def _candle_record(payload: object) -> TossCandleRecord:
    if not isinstance(payload, Mapping):
        raise ValueError("Toss candle record is invalid")
    payload = cast(Mapping[str, object], payload)
    timestamp = _provider_datetime(payload.get("timestamp"), name="timestamp")
    currency = payload.get("currency")
    if not isinstance(currency, str):
        raise ValueError("Toss candle currency is invalid")
    return TossCandleRecord(
        timestamp=timestamp,
        open_price=_provider_decimal(payload.get("openPrice"), name="openPrice"),
        high_price=_provider_decimal(payload.get("highPrice"), name="highPrice"),
        low_price=_provider_decimal(payload.get("lowPrice"), name="lowPrice"),
        close_price=_provider_decimal(payload.get("closePrice"), name="closePrice"),
        volume=_provider_decimal(payload.get("volume"), name="volume"),
        currency=currency,
    )


def _price_record(payload: object) -> TossPriceRecord:
    if not isinstance(payload, Mapping):
        raise ValueError("Toss price record is invalid")
    payload = cast(Mapping[str, object], payload)
    symbol = payload.get("symbol")
    timestamp = payload.get("timestamp")
    currency = payload.get("currency")
    if not isinstance(symbol, str) or not isinstance(currency, str):
        raise ValueError("Toss price record is invalid")
    if timestamp is not None:
        timestamp = _provider_datetime(timestamp, name="timestamp", kind="price")
    return TossPriceRecord(
        symbol=symbol,
        timestamp=timestamp,
        last_price=_provider_decimal(
            payload.get("lastPrice"), name="lastPrice", kind="price"
        ),
        currency=currency,
    )


def _toss_account(payload: object) -> TossAccount:
    if not isinstance(payload, Mapping):
        raise ValueError("Toss account record is invalid")
    payload = cast(Mapping[str, object], payload)
    account_number = payload.get("accountNo")
    account_seq = payload.get("accountSeq")
    account_type = payload.get("accountType")
    if (
        not isinstance(account_number, str)
        or not account_number
        or "\n" in account_number
        or not isinstance(account_type, str)
        or not account_type
        or "\n" in account_type
    ):
        raise ValueError("Toss account record is invalid")
    return TossAccount(account_seq=cast(int, account_seq), account_type=account_type)


def _provider_datetime(value: object, *, name: str, kind: str = "candle") -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Toss {kind} {name} is invalid")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Toss {kind} {name} is invalid") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"Toss {kind} timestamp is invalid")
    return timestamp


def _provider_decimal(value: object, *, name: str, kind: str = "candle") -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"Toss {kind} {name} is invalid")
    try:
        return parse_contract_decimal(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Toss {kind} {name} is invalid") from error
