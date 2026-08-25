from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from urllib.parse import urlencode

from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerMarket,
    BrokerRequest,
    BrokerResponse,
    UnsupportedBrokerMarket,
)
from autotrader.integrations.brokers.toss.market_data_contracts import (
    TossCandleInterval,
    TossCandlePage,
    TossCandleRecord,
)
from autotrader.shared.decimal import parse_contract_decimal


class TossMarketDataAuthenticationError(RuntimeError):
    """Raised when the read-only Toss OAuth response is invalid."""


class TossIncompleteCandleSnapshot(RuntimeError):
    """Raised when Toss requires a page outside the declared read limit."""


@dataclass(frozen=True, slots=True)
class TossClientCredentials:
    client_id: str
    client_secret: str

    def __post_init__(self) -> None:
        if any(
            not value or "\n" in value for value in (self.client_id, self.client_secret)
        ):
            raise ValueError("Toss credentials must be non-empty single lines")


@dataclass(frozen=True, slots=True)
class TossAccessToken:
    value: str
    expires_in_seconds: int

    def __post_init__(self) -> None:
        if not self.value or "\n" in self.value:
            raise ValueError("Toss access token must be a non-empty single line")
        if type(self.expires_in_seconds) is not int or self.expires_in_seconds <= 0:
            raise ValueError("Toss access token expiry must be positive")


class TossMarketDataReadOnlyAdapter:
    """Constructs only Toss OAuth and completed-candle read requests."""

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
            raise TossMarketDataAuthenticationError("Toss OAuth request failed")
        return _decode_access_token(response.body)

    async def read_complete_candle_pages(
        self,
        *,
        market: BrokerMarket,
        symbol: str,
        interval: TossCandleInterval,
        count: int,
        before: datetime,
        adjusted: bool,
        access_token: str,
        max_pages: int,
    ) -> tuple[TossCandlePage, ...]:
        if (
            market is not BrokerMarket.KRX_STOCK
            or interval is not TossCandleInterval.ONE_MINUTE
        ):
            raise UnsupportedBrokerMarket("Toss market-data reader requires KRX 1m")
        if not _symbol(symbol) or not 1 <= count <= 200 or not 1 <= max_pages <= 10:
            raise ValueError("Toss candle request is invalid")
        if before.tzinfo is not UTC or before.utcoffset() != UTC.utcoffset(before):
            raise ValueError("Toss candle before must be UTC")
        if not access_token or "\n" in access_token:
            raise ValueError("Toss candle request is invalid")
        cursor: datetime | None = before
        pages: list[TossCandlePage] = []
        for _ in range(max_pages):
            response = await self._transport.request(
                BrokerRequest(
                    method="GET",
                    path=_candle_path(
                        symbol=symbol,
                        interval=interval,
                        count=count,
                        before=cursor,
                        adjusted=adjusted,
                    ),
                    headers=(("Authorization", f"Bearer {access_token}"),),
                )
            )
            page = decode_toss_candle_page(response)
            pages.append(page)
            if page.next_before is None:
                return tuple(pages)
            next_before = _provider_datetime(page.next_before, name="nextBefore")
            if not page.records or next_before >= cursor:
                raise TossIncompleteCandleSnapshot("Toss candle cursor did not advance")
            cursor = next_before
        raise TossIncompleteCandleSnapshot("Toss candle pages exceeded read limit")

    async def read_recent_candle_pages(
        self,
        *,
        market: BrokerMarket,
        symbol: str,
        interval: TossCandleInterval,
        count: int,
        before: datetime,
        adjusted: bool,
        access_token: str,
        max_pages: int,
    ) -> tuple[TossCandlePage, ...]:
        if (
            market is not BrokerMarket.KRX_STOCK
            or interval is not TossCandleInterval.ONE_MINUTE
        ):
            raise UnsupportedBrokerMarket("Toss market-data reader requires KRX 1m")
        if (
            not _symbol(symbol)
            or not 1 <= count <= 200
            or type(max_pages) is not int
            or not 1 <= max_pages <= 10
        ):
            raise ValueError("Toss candle request is invalid")
        if before.tzinfo is not UTC or before.utcoffset() != UTC.utcoffset(before):
            raise ValueError("Toss candle before must be UTC")
        if not access_token or "\n" in access_token:
            raise ValueError("Toss candle request is invalid")
        cursor: datetime | str = before
        parsed_cursor = before
        pages: list[TossCandlePage] = []
        for _ in range(max_pages):
            response = await self._transport.request(
                BrokerRequest(
                    method="GET",
                    path=_candle_path(
                        symbol=symbol,
                        interval=interval,
                        count=count,
                        before=cursor,
                        adjusted=adjusted,
                    ),
                    headers=(("Authorization", f"Bearer {access_token}"),),
                )
            )
            page = decode_toss_candle_page(response)
            pages.append(page)
            if page.next_before is None:
                return tuple(pages)
            next_before = _provider_datetime(page.next_before, name="nextBefore")
            if not page.records or next_before >= parsed_cursor:
                raise TossIncompleteCandleSnapshot("Toss candle cursor did not advance")
            if len(pages) == max_pages:
                return tuple(pages)
            cursor = page.next_before
            parsed_cursor = next_before
        raise AssertionError("unreachable")


def decode_toss_candle_page(response: BrokerResponse) -> TossCandlePage:
    if response.status != 200:
        raise ValueError("Toss candle response is not successful")
    try:
        payload: object = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Toss candle response is not valid JSON") from error
    if not isinstance(payload, Mapping):
        raise ValueError("Toss candle response is invalid")
    values = cast(Mapping[str, object], payload)
    result_value = values.get("result")
    if not isinstance(result_value, Mapping):
        raise ValueError("Toss candle response is invalid")
    result = cast(Mapping[str, object], result_value)
    candles = result.get("candles")
    next_before = result.get("nextBefore")
    if not isinstance(candles, list) or (
        next_before is not None and not isinstance(next_before, str)
    ):
        raise ValueError("Toss candle response is invalid")
    return TossCandlePage(
        records=tuple(_candle_record(value) for value in cast(list[object], candles)),
        next_before=next_before,
    )


def _decode_access_token(body: bytes) -> TossAccessToken:
    try:
        payload: object = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TossMarketDataAuthenticationError(
            "Toss OAuth response is invalid"
        ) from error
    if not isinstance(payload, Mapping):
        raise TossMarketDataAuthenticationError("Toss OAuth response is invalid")
    values = cast(Mapping[str, object], payload)
    token = values.get("access_token")
    expires = values.get("expires_in")
    if (
        not isinstance(token, str)
        or values.get("token_type") != "Bearer"
        or type(expires) is not int
    ):
        raise TossMarketDataAuthenticationError("Toss OAuth response is invalid")
    return TossAccessToken(value=token, expires_in_seconds=expires)


def _candle_path(
    *,
    symbol: str,
    interval: TossCandleInterval,
    count: int,
    before: datetime | str | None,
    adjusted: bool,
) -> str:
    query = [("symbol", symbol), ("interval", interval.value), ("count", str(count))]
    if before is not None:
        before_value = (
            before.isoformat().replace("+00:00", "Z")
            if isinstance(before, datetime)
            else before
        )
        query.append(("before", before_value))
    query.append(("adjusted", str(adjusted).lower()))
    return f"/api/v1/candles?{urlencode(query)}"


def _candle_record(value: object) -> TossCandleRecord:
    if not isinstance(value, Mapping):
        raise ValueError("Toss candle record is invalid")
    fields = cast(Mapping[str, object], value)
    return TossCandleRecord(
        timestamp=_provider_datetime(fields.get("timestamp"), name="timestamp"),
        open_price=_decimal(fields.get("openPrice")),
        high_price=_decimal(fields.get("highPrice")),
        low_price=_decimal(fields.get("lowPrice")),
        close_price=_decimal(fields.get("closePrice")),
        volume=_decimal(fields.get("volume")),
        currency=_currency(fields.get("currency")),
    )


def _provider_datetime(value: object, *, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Toss candle {name} is invalid")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Toss candle {name} is invalid") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"Toss candle {name} is invalid")
    return timestamp


def _decimal(value: object) -> Decimal:
    try:
        return parse_contract_decimal(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Toss candle price is invalid") from error


def _currency(value: object) -> str:
    if not isinstance(value, str) or not value or "\n" in value:
        raise ValueError("Toss candle currency is invalid")
    return value


def _symbol(value: str) -> bool:
    return len(value) == 6 and value.isascii() and value.isdecimal()
