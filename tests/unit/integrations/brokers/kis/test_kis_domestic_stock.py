from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from autotrader.integrations.brokers.common import (
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)
from autotrader.integrations.brokers.kis import domestic_stock
from autotrader.integrations.brokers.kis.adapter import KisReadCredentials
from autotrader.integrations.brokers.kis.domestic_stock import (
    KisDomesticDailyChartPage,
    KisDomesticDailyRecord,
    KisDomesticMarket,
    KisDomesticMinuteChartPage,
    KisDomesticMinuteRecord,
    KisDomesticPriceRecord,
    KisDomesticStockReadOnlyAdapter,
    KisIncompleteDailyChartSnapshot,
    decode_kis_domestic_daily_chart,
    decode_kis_domestic_minute_chart,
    decode_kis_domestic_price,
)


@dataclass
class RecordingTransport:
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])
    response: BrokerResponse = field(
        default_factory=lambda: BrokerResponse(status=200, body=b"{}")
    )

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return self.response


@dataclass
class ScriptedTransport:
    responses: tuple[BrokerResponse, ...]
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return self.responses[len(self.requests) - 1]


def credentials() -> KisReadCredentials:
    return KisReadCredentials(access_token="token", app_key="app", app_secret="secret")


@pytest.mark.asyncio
async def test_kis_domestic_daily_chart_uses_documented_query_and_tr_id() -> None:
    assert hasattr(domestic_stock, "KisDomesticPriceBasis")
    transport = RecordingTransport(
        response=BrokerResponse(
            status=200,
            body=(
                b'{"rt_cd":"0","output2":[{"stck_bsop_date":"20260810",'
                b'"stck_oprc":"99","stck_hgpr":"103","stck_lwpr":"98",'
                b'"stck_clpr":"102","acml_vol":"1200"}]}'
            ),
        )
    )
    adapter = KisDomesticStockReadOnlyAdapter(transport=transport)

    pages = await adapter.read_complete_daily_chart(
        credentials=credentials(),
        market=KisDomesticMarket.KRX,
        symbol="005930",
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 10),
        price_basis=domestic_stock.KisDomesticPriceBasis.ADJUSTED,
        max_pages=1,
    )

    assert pages[0].records[0].close_price == Decimal("102")
    assert transport.requests[0].path == (
        "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice?"
        "FID_COND_MRKT_DIV_CODE=J&FID_INPUT_ISCD=005930&"
        "FID_INPUT_DATE_1=20260810&FID_INPUT_DATE_2=20260810&"
        "FID_PERIOD_DIV_CODE=D&FID_ORG_ADJ_PRC=0"
    )
    assert dict(transport.requests[0].headers)["tr_id"] == "FHKST03010100"


@pytest.mark.asyncio
async def test_kis_domestic_daily_chart_advances_with_an_inclusive_oldest_date() -> (
    None
):
    first = BrokerResponse(
        status=200,
        body=(
            b'{"rt_cd":"0","output2":[{"stck_bsop_date":"20260812",'
            b'"stck_oprc":"99","stck_hgpr":"103","stck_lwpr":"98",'
            b'"stck_clpr":"102","acml_vol":"1200"},{"stck_bsop_date":"20260811",'
            b'"stck_oprc":"99","stck_hgpr":"103","stck_lwpr":"98",'
            b'"stck_clpr":"102","acml_vol":"1200"}]}'
        ),
    )
    second = BrokerResponse(
        status=200,
        body=(
            b'{"rt_cd":"0","output2":[{"stck_bsop_date":"20260811",'
            b'"stck_oprc":"99","stck_hgpr":"103","stck_lwpr":"98",'
            b'"stck_clpr":"102","acml_vol":"1200"},{"stck_bsop_date":"20260810",'
            b'"stck_oprc":"99","stck_hgpr":"103","stck_lwpr":"98",'
            b'"stck_clpr":"102","acml_vol":"1200"}]}'
        ),
    )
    transport = ScriptedTransport(responses=(first, second))

    pages = await KisDomesticStockReadOnlyAdapter(
        transport=transport
    ).read_complete_daily_chart(
        credentials=credentials(),
        market=KisDomesticMarket.KRX,
        symbol="005930",
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 12),
        price_basis=domestic_stock.KisDomesticPriceBasis.ORIGINAL,
        max_pages=2,
    )

    assert [page.records[-1].trading_date for page in pages] == [
        date(2026, 8, 11),
        date(2026, 8, 10),
    ]
    assert "FID_INPUT_DATE_2=20260811" in transport.requests[1].path


@pytest.mark.asyncio
async def test_kis_domestic_daily_chart_spaces_provider_directed_continuations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = BrokerResponse(
        status=200,
        body=(
            b'{"rt_cd":"0","output2":[{"stck_bsop_date":"20260812",'
            b'"stck_oprc":"99","stck_hgpr":"103","stck_lwpr":"98",'
            b'"stck_clpr":"102","acml_vol":"1200"},{"stck_bsop_date":"20260811",'
            b'"stck_oprc":"99","stck_hgpr":"103","stck_lwpr":"98",'
            b'"stck_clpr":"102","acml_vol":"1200"}]}'
        ),
    )
    second = BrokerResponse(
        status=200,
        body=(
            b'{"rt_cd":"0","output2":[{"stck_bsop_date":"20260810",'
            b'"stck_oprc":"99","stck_hgpr":"103","stck_lwpr":"98",'
            b'"stck_clpr":"102","acml_vol":"1200"}]}'
        ),
    )
    transport = ScriptedTransport(responses=(first, second))
    sleeps: list[float] = []
    events: list[str] = []

    original_request = transport.request

    async def record_request(request: BrokerRequest) -> BrokerResponse:
        events.append("request")
        return await original_request(request)

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)
        events.append(f"sleep:{delay}")

    monkeypatch.setattr(transport, "request", record_request)
    monkeypatch.setattr(
        domestic_stock,
        "asyncio",
        SimpleNamespace(sleep=record_sleep),
        raising=False,
    )

    pages = await KisDomesticStockReadOnlyAdapter(
        transport=transport
    ).read_complete_daily_chart(
        credentials=credentials(),
        market=KisDomesticMarket.KRX,
        symbol="005930",
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 12),
        price_basis=domestic_stock.KisDomesticPriceBasis.ORIGINAL,
        max_pages=2,
    )

    assert len(pages) == 2
    assert sleeps == [0.1]
    assert events == ["request", "sleep:0.1", "request"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_date", "end_date", "price_basis", "max_pages"),
    [
        (
            date(2026, 8, 11),
            date(2026, 8, 10),
            domestic_stock.KisDomesticPriceBasis.ADJUSTED,
            1,
        ),
        (
            datetime(2026, 8, 10),
            date(2026, 8, 10),
            domestic_stock.KisDomesticPriceBasis.ADJUSTED,
            1,
        ),
        (date(2026, 8, 10), date(2026, 8, 10), "0", 1),
        (
            date(2026, 8, 10),
            date(2026, 8, 10),
            domestic_stock.KisDomesticPriceBasis.ADJUSTED,
            0,
        ),
        (
            date(2026, 8, 10),
            date(2026, 8, 10),
            domestic_stock.KisDomesticPriceBasis.ADJUSTED,
            True,
        ),
    ],
)
async def test_kis_domestic_daily_chart_rejects_invalid_bounds_before_transport(
    start_date: object, end_date: object, price_basis: object, max_pages: object
) -> None:
    transport = RecordingTransport()
    adapter = KisDomesticStockReadOnlyAdapter(transport=transport)

    with pytest.raises(ValueError):
        await adapter.read_complete_daily_chart(
            credentials=credentials(),
            market=KisDomesticMarket.KRX,
            symbol="005930",
            start_date=start_date,
            end_date=end_date,
            price_basis=price_basis,
            max_pages=max_pages,
        )

    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses",
    [
        (BrokerResponse(status=200, body=b'{"rt_cd":"0","output2":[]}'),),
        (
            BrokerResponse(
                status=200,
                body=(
                    b'{"rt_cd":"0","output2":[{"stck_bsop_date":"20260811",'
                    b'"stck_oprc":"99","stck_hgpr":"103","stck_lwpr":"98",'
                    b'"stck_clpr":"102","acml_vol":"1200"}]}'
                ),
            ),
            BrokerResponse(
                status=200,
                body=(
                    b'{"rt_cd":"0","output2":[{"stck_bsop_date":"20260811",'
                    b'"stck_oprc":"99","stck_hgpr":"103","stck_lwpr":"98",'
                    b'"stck_clpr":"102","acml_vol":"1200"}]}'
                ),
            ),
        ),
    ],
)
async def test_kis_domestic_daily_chart_rejects_incomplete_snapshots(
    responses: tuple[BrokerResponse, ...],
) -> None:
    with pytest.raises(
        KisIncompleteDailyChartSnapshot, match="KIS daily chart snapshot is incomplete"
    ):
        await KisDomesticStockReadOnlyAdapter(
            transport=ScriptedTransport(responses=responses)
        ).read_complete_daily_chart(
            credentials=credentials(),
            market=KisDomesticMarket.KRX,
            symbol="005930",
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 12),
            price_basis=domestic_stock.KisDomesticPriceBasis.ADJUSTED,
            max_pages=2,
        )


@pytest.mark.asyncio
async def test_kis_domestic_daily_chart_rejects_page_limit_exhaustion() -> None:
    response = BrokerResponse(
        status=200,
        body=(
            b'{"rt_cd":"0","output2":[{"stck_bsop_date":"20260811",'
            b'"stck_oprc":"99","stck_hgpr":"103","stck_lwpr":"98",'
            b'"stck_clpr":"102","acml_vol":"1200"}]}'
        ),
    )

    with pytest.raises(
        KisIncompleteDailyChartSnapshot, match="KIS daily chart snapshot is incomplete"
    ):
        await KisDomesticStockReadOnlyAdapter(
            transport=ScriptedTransport(responses=(response,))
        ).read_complete_daily_chart(
            credentials=credentials(),
            market=KisDomesticMarket.KRX,
            symbol="005930",
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 12),
            price_basis=domestic_stock.KisDomesticPriceBasis.ADJUSTED,
            max_pages=1,
        )


@pytest.mark.parametrize(
    "body",
    [
        b'{"rt_cd":"1","output2":[]}',
        b'{"rt_cd":"0","output2":{}}',
        b'{"rt_cd":"0","output2":[{"stck_bsop_date":"20260810",'
        b'"stck_oprc":"99","stck_hgpr":"103","stck_lwpr":"98",'
        b'"stck_clpr":"102"}]}',
    ],
)
def test_kis_domestic_daily_chart_decoder_rejects_malformed_data(body: bytes) -> None:
    with pytest.raises(ValueError):
        decode_kis_domestic_daily_chart(BrokerResponse(status=200, body=body))


def test_kis_domestic_daily_records_require_valid_immutable_ohlcv() -> None:
    record = KisDomesticDailyRecord(
        trading_date=date(2026, 8, 10),
        open_price=Decimal("99"),
        high_price=Decimal("103"),
        low_price=Decimal("98"),
        close_price=Decimal("102"),
        volume=Decimal("1200"),
    )
    assert KisDomesticDailyChartPage(records=(record,)).records == (record,)
    with pytest.raises(ValueError, match="range"):
        KisDomesticDailyRecord(
            trading_date=date(2026, 8, 10),
            open_price=Decimal("97"),
            high_price=Decimal("103"),
            low_price=Decimal("98"),
            close_price=Decimal("102"),
            volume=Decimal("1200"),
        )
    with pytest.raises(ValueError, match="immutable"):
        KisDomesticDailyChartPage(records=[record])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_kis_domestic_price_uses_documented_market_code_and_tr_id() -> None:
    transport = RecordingTransport()
    adapter = KisDomesticStockReadOnlyAdapter(transport=transport)

    await adapter.read_price(
        credentials=credentials(),
        market=KisDomesticMarket.KRX,
        symbol="005930",
    )

    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path=(
                "/uapi/domestic-stock/v1/quotations/"
                "inquire-price?FID_COND_MRKT_DIV_CODE=J&FID_INPUT_ISCD=005930"
            ),
            headers=(
                ("authorization", "Bearer token"),
                ("appkey", "app"),
                ("appsecret", "secret"),
                ("tr_id", "FHKST01010100"),
                ("custtype", "P"),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_kis_domestic_daily_minutes_use_date_time_cursor_without_account() -> (
    None
):
    transport = RecordingTransport()
    adapter = KisDomesticStockReadOnlyAdapter(transport=transport)

    await adapter.read_daily_minutes(
        credentials=credentials(),
        market=KisDomesticMarket.KRX,
        symbol="005930",
        cursor_date=date(2026, 8, 10),
        cursor_time=time(13, 0),
        include_previous_data=True,
    )

    request = transport.requests[0]
    assert request.path == (
        "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice?"
        "FID_COND_MRKT_DIV_CODE=J&FID_INPUT_ISCD=005930&FID_INPUT_HOUR_1=130000&"
        "FID_INPUT_DATE_1=20260810&FID_PW_DATA_INCU_YN=Y&FID_FAKE_TICK_INCU_YN="
    )
    assert "CANO" not in request.path
    assert "ACNT_PRDT_CD" not in request.path
    assert dict(request.headers)["tr_id"] == "FHKST03010230"


def test_kis_domestic_decoders_preserve_provider_local_price_and_minute_fields() -> (
    None
):
    price = decode_kis_domestic_price(
        BrokerResponse(
            status=200,
            body=(
                b'{"rt_cd":"0","output":{"stck_prpr":"100",'
                b'"stck_oprc":"99","stck_hgpr":"101",'
                b'"stck_lwpr":"98","acml_vol":"1000"}}'
            ),
        )
    )
    minutes = decode_kis_domestic_minute_chart(
        BrokerResponse(
            status=200,
            body=(
                b'{"rt_cd":"0","output2":[{"stck_bsop_date":"20260810",'
                b'"stck_cntg_hour":"090500","stck_oprc":"100",'
                b'"stck_hgpr":"103","stck_lwpr":"99",'
                b'"stck_prpr":"102","cntg_vol":"1200"}]}'
            ),
        )
    )

    assert price == KisDomesticPriceRecord(
        open_price=Decimal("99"),
        high_price=Decimal("101"),
        low_price=Decimal("98"),
        last_price=Decimal("100"),
        cumulative_volume=Decimal("1000"),
    )
    assert minutes == KisDomesticMinuteChartPage(
        records=(
            KisDomesticMinuteRecord(
                trading_date=date(2026, 8, 10),
                trading_time=time(9, 5),
                open_price=Decimal("100"),
                high_price=Decimal("103"),
                low_price=Decimal("99"),
                close_price=Decimal("102"),
                volume=Decimal("1200"),
            ),
        )
    )


def test_kis_domestic_decoder_rejects_unsuccessful_or_invalid_envelopes() -> None:
    with pytest.raises(ValueError, match="successful"):
        decode_kis_domestic_minute_chart(
            BrokerResponse(status=200, body=b'{"rt_cd":"1"}')
        )
    with pytest.raises(ValueError, match="range"):
        decode_kis_domestic_price(
            BrokerResponse(
                status=200,
                body=(
                    b'{"rt_cd":"0","output":{"stck_prpr":"100",'
                    b'"stck_oprc":"99","stck_hgpr":"98",'
                    b'"stck_lwpr":"98","acml_vol":"1000"}}'
                ),
            )
        )


@pytest.mark.asyncio
async def test_kis_domestic_adapter_has_no_submit_cancel_or_replace_route() -> None:
    adapter = KisDomesticStockReadOnlyAdapter(transport=RecordingTransport())

    with pytest.raises(BrokerWriteDisabled, match="not enabled"):
        await adapter.submit(command=object())
    with pytest.raises(BrokerWriteDisabled, match="not enabled"):
        await adapter.cancel(command=object())
    with pytest.raises(BrokerWriteDisabled, match="not enabled"):
        await adapter.replace(command=object())
