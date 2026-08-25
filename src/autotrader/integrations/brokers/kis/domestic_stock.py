from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from typing import NoReturn, cast
from urllib.parse import urlencode

from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)
from autotrader.integrations.brokers.kis.domestic_stock_contracts import (
    KisDomesticDailyChartPage,
    KisDomesticDailyRecord,
    KisDomesticMarket,
    KisDomesticMinuteChartPage,
    KisDomesticMinuteRecord,
    KisDomesticPriceBasis,
    KisDomesticPriceRecord,
)
from autotrader.integrations.brokers.kis.read_contracts import KisReadCredentials
from autotrader.shared.decimal import parse_contract_decimal


class KisIncompleteDailyChartSnapshot(RuntimeError):
    pass


class KisDomesticStockReadOnlyAdapter:
    """Builds authenticated KIS domestic-stock market-data reads only."""

    _price_path = "/uapi/domestic-stock/v1/quotations/inquire-price"
    _price_tr_id = "FHKST01010100"
    _daily_minutes_path = (
        "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
    )
    _daily_minutes_tr_id = "FHKST03010230"
    _daily_chart_path = (
        "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    )
    _daily_chart_tr_id = "FHKST03010100"

    def __init__(self, *, transport: AsyncHttpTransport) -> None:
        self._transport = transport

    async def read_price(
        self,
        *,
        credentials: KisReadCredentials,
        market: KisDomesticMarket,
        symbol: str,
    ) -> BrokerResponse:
        query = urlencode(
            (
                ("FID_COND_MRKT_DIV_CODE", _market(market).value),
                ("FID_INPUT_ISCD", _symbol(symbol)),
            )
        )
        return await self._transport.request(
            BrokerRequest(
                method="GET",
                path=f"{self._price_path}?{query}",
                headers=_headers(credentials, self._price_tr_id),
            )
        )

    async def read_daily_minutes(
        self,
        *,
        credentials: KisReadCredentials,
        market: KisDomesticMarket,
        symbol: str,
        cursor_date: date,
        cursor_time: time,
        include_previous_data: bool,
    ) -> BrokerResponse:
        normalized_date = _cursor_date(cursor_date)
        normalized_time = _cursor_time(cursor_time)
        normalized_include_previous_data = cast(object, include_previous_data)
        if not isinstance(normalized_include_previous_data, bool):
            raise ValueError("include_previous_data must be a bool")
        query = urlencode(
            (
                ("FID_COND_MRKT_DIV_CODE", _market(market).value),
                ("FID_INPUT_ISCD", _symbol(symbol)),
                ("FID_INPUT_HOUR_1", normalized_time.strftime("%H%M%S")),
                ("FID_INPUT_DATE_1", normalized_date.strftime("%Y%m%d")),
                (
                    "FID_PW_DATA_INCU_YN",
                    "Y" if normalized_include_previous_data else "N",
                ),
                ("FID_FAKE_TICK_INCU_YN", ""),
            )
        )
        return await self._transport.request(
            BrokerRequest(
                method="GET",
                path=f"{self._daily_minutes_path}?{query}",
                headers=_headers(credentials, self._daily_minutes_tr_id),
            )
        )

    async def read_complete_daily_chart(
        self,
        *,
        credentials: KisReadCredentials,
        market: KisDomesticMarket,
        symbol: str,
        start_date: date,
        end_date: date,
        price_basis: KisDomesticPriceBasis,
        max_pages: int,
    ) -> tuple[KisDomesticDailyChartPage, ...]:
        start_date = _daily_chart_date(start_date)
        end_date = _daily_chart_date(end_date)
        if start_date > end_date:
            raise ValueError("KIS domestic daily chart date range is invalid")
        market = _market(market)
        symbol = _symbol(symbol)
        if type(price_basis) is not KisDomesticPriceBasis:
            raise ValueError("KIS domestic daily chart price basis is invalid")
        normalized_max_pages = cast(object, max_pages)
        if (
            not isinstance(normalized_max_pages, int)
            or isinstance(normalized_max_pages, bool)
            or not 1 <= normalized_max_pages <= 10
        ):
            raise ValueError("KIS domestic daily chart page limit is invalid")

        pages: list[KisDomesticDailyChartPage] = []
        request_end_date = end_date
        prior_boundary: date | None = None
        for page_index in range(max_pages):
            query = urlencode(
                (
                    ("FID_COND_MRKT_DIV_CODE", market.value),
                    ("FID_INPUT_ISCD", symbol),
                    ("FID_INPUT_DATE_1", start_date.strftime("%Y%m%d")),
                    ("FID_INPUT_DATE_2", request_end_date.strftime("%Y%m%d")),
                    ("FID_PERIOD_DIV_CODE", "D"),
                    ("FID_ORG_ADJ_PRC", price_basis.value),
                )
            )
            response = await self._transport.request(
                BrokerRequest(
                    method="GET",
                    path=f"{self._daily_chart_path}?{query}",
                    headers=_headers(credentials, self._daily_chart_tr_id),
                )
            )
            try:
                page = decode_kis_domestic_daily_chart(response)
            except ValueError as error:
                raise KisIncompleteDailyChartSnapshot(
                    "KIS daily chart snapshot is incomplete"
                ) from error
            if not page.records:
                raise KisIncompleteDailyChartSnapshot(
                    "KIS daily chart snapshot is incomplete"
                )
            boundary = min(record.trading_date for record in page.records)
            if prior_boundary is not None and boundary >= prior_boundary:
                raise KisIncompleteDailyChartSnapshot(
                    "KIS daily chart snapshot is incomplete"
                )
            pages.append(page)
            if boundary <= start_date:
                return tuple(pages)
            prior_boundary = boundary
            request_end_date = boundary
            if page_index + 1 < max_pages:
                await asyncio.sleep(0.1)
        raise KisIncompleteDailyChartSnapshot("KIS daily chart snapshot is incomplete")

    async def submit(self, *, command: object) -> NoReturn:
        del command
        raise BrokerWriteDisabled("KIS domestic write adapter is not enabled")

    async def cancel(self, *, command: object) -> NoReturn:
        del command
        raise BrokerWriteDisabled("KIS domestic write adapter is not enabled")

    async def replace(self, *, command: object) -> NoReturn:
        del command
        raise BrokerWriteDisabled("KIS domestic write adapter is not enabled")


def decode_kis_domestic_price(response: BrokerResponse) -> KisDomesticPriceRecord:
    output = _successful_output(response, key="output")
    return KisDomesticPriceRecord(
        open_price=_decimal(output, "stck_oprc"),
        high_price=_decimal(output, "stck_hgpr"),
        low_price=_decimal(output, "stck_lwpr"),
        last_price=_decimal(output, "stck_prpr"),
        cumulative_volume=_decimal(output, "acml_vol"),
    )


def decode_kis_domestic_minute_chart(
    response: BrokerResponse,
) -> KisDomesticMinuteChartPage:
    payload = _successful_payload(response)
    records = payload.get("output2")
    if not isinstance(records, list):
        raise ValueError("KIS domestic minute response is incomplete")
    raw_records = cast(list[object], records)
    return KisDomesticMinuteChartPage(
        records=tuple(_minute_record(record) for record in raw_records)
    )


def decode_kis_domestic_daily_chart(
    response: BrokerResponse,
) -> KisDomesticDailyChartPage:
    payload = _successful_payload(response)
    records = payload.get("output2")
    if not isinstance(records, list):
        raise ValueError("KIS domestic daily response is incomplete")
    raw_records = cast(list[object], records)
    return KisDomesticDailyChartPage(
        records=tuple(_daily_record(record) for record in raw_records)
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


def _market(value: object) -> KisDomesticMarket:
    if not isinstance(value, KisDomesticMarket):
        raise ValueError("KIS domestic market is invalid")
    return value


def _symbol(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 6
        or not value.isascii()
        or not value.isdecimal()
    ):
        raise ValueError("KIS domestic stock symbol must be six digits")
    return value


def _cursor_date(value: object) -> date:
    if type(value) is not date:
        raise ValueError("KIS domestic minute cursor date is invalid")
    return value


def _cursor_time(value: object) -> time:
    if type(value) is not time or value.microsecond != 0:
        raise ValueError("KIS domestic minute cursor time is invalid")
    return value


def _daily_chart_date(value: object) -> date:
    if type(value) is not date:
        raise ValueError("KIS domestic daily chart date is invalid")
    return value


def _successful_output(response: BrokerResponse, *, key: str) -> Mapping[str, object]:
    output = _successful_payload(response).get(key)
    if not isinstance(output, Mapping):
        raise ValueError("KIS domestic response is incomplete")
    return cast(Mapping[str, object], output)


def _successful_payload(response: BrokerResponse) -> Mapping[str, object]:
    if response.status != 200:
        raise ValueError("KIS domestic response was not successful")
    try:
        payload = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("KIS domestic response is not valid JSON") from error
    if not isinstance(payload, Mapping):
        raise ValueError("KIS domestic response was not successful")
    typed_payload = cast(Mapping[str, object], payload)
    if typed_payload.get("rt_cd") != "0":
        raise ValueError("KIS domestic response was not successful")
    return typed_payload


def _minute_record(payload: object) -> KisDomesticMinuteRecord:
    if not isinstance(payload, Mapping):
        raise ValueError("KIS domestic minute record is invalid")
    record = cast(Mapping[str, object], payload)
    return KisDomesticMinuteRecord(
        trading_date=_provider_date(record, "stck_bsop_date"),
        trading_time=_provider_time(record, "stck_cntg_hour"),
        open_price=_decimal(record, "stck_oprc"),
        high_price=_decimal(record, "stck_hgpr"),
        low_price=_decimal(record, "stck_lwpr"),
        close_price=_decimal(record, "stck_prpr"),
        volume=_decimal(record, "cntg_vol"),
    )


def _daily_record(payload: object) -> KisDomesticDailyRecord:
    if not isinstance(payload, Mapping):
        raise ValueError("KIS domestic daily record is invalid")
    record = cast(Mapping[str, object], payload)
    return KisDomesticDailyRecord(
        trading_date=_provider_date(record, "stck_bsop_date"),
        open_price=_decimal(record, "stck_oprc"),
        high_price=_decimal(record, "stck_hgpr"),
        low_price=_decimal(record, "stck_lwpr"),
        close_price=_decimal(record, "stck_clpr"),
        volume=_decimal(record, "acml_vol"),
    )


def _provider_date(payload: Mapping[str, object], key: str) -> date:
    value = _text(payload, key)
    try:
        parsed = datetime.strptime(value, "%Y%m%d").date()
    except ValueError as error:
        raise ValueError("KIS domestic date is invalid") from error
    if parsed.strftime("%Y%m%d") != value:
        raise ValueError("KIS domestic date is invalid")
    return parsed


def _provider_time(payload: Mapping[str, object], key: str) -> time:
    value = _text(payload, key)
    try:
        parsed = datetime.strptime(value, "%H%M%S").time()
    except ValueError as error:
        raise ValueError("KIS domestic time is invalid") from error
    if parsed.strftime("%H%M%S") != value:
        raise ValueError("KIS domestic time is invalid")
    return parsed


def _decimal(payload: Mapping[str, object], key: str) -> Decimal:
    return parse_contract_decimal(_text(payload, key))


def _text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or "\n" in value:
        raise ValueError(f"KIS domestic {key} is invalid")
    return value
