from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from hashlib import sha256
from urllib.parse import urlencode
from uuid import UUID, uuid7

import pytest

from autotrader.integrations.brokers.common import (
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
    UnsupportedBrokerInstrument,
)
from autotrader.integrations.brokers.kis.adapter import (
    KisAccessToken,
    KisAccountReadCredentials,
    KisAuthenticationError,
    KisClientCredentials,
    KisContractDetailPage,
    KisFuturesPricePage,
    KisFuturesPriceRecord,
    KisIncompleteAccountSnapshot,
    KisIncompleteContractSnapshot,
    KisIncompleteMinuteChartSnapshot,
    KisMinuteChartInterval,
    KisMinuteChartPage,
    KisMinuteChartRecord,
    KisReadCredentials,
    KisReadOnlyAdapter,
    decode_kis_contract_detail_page,
    decode_kis_futures_price_page,
    decode_kis_minute_chart_page,
    issue_kis_access_token,
)
from autotrader.integrations.brokers.kis.contracts import KisActiveContract
from autotrader.integrations.brokers.kis.oauth import (
    KisAccessToken as NeutralKisAccessToken,
)
from autotrader.integrations.brokers.kis.oauth import (
    KisAuthenticationError as NeutralKisAuthenticationError,
)
from autotrader.integrations.brokers.kis.oauth import (
    KisClientCredentials as NeutralKisClientCredentials,
)
from autotrader.integrations.brokers.kis.oauth import (
    issue_kis_access_token as neutral_issue_kis_access_token,
)


def test_legacy_oauth_exports_neutral_oauth_api() -> None:
    assert KisAccessToken is NeutralKisAccessToken
    assert KisAuthenticationError is NeutralKisAuthenticationError
    assert KisClientCredentials is NeutralKisClientCredentials
    assert issue_kis_access_token is neutral_issue_kis_access_token


@dataclass
class RecordingTransport:
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return BrokerResponse(status=200, body=b"{}")


@dataclass
class TokenTransport:
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])
    body: bytes = (
        b'{"access_token":"issued-token",'
        b'"access_token_token_expired":"2026-08-11 09:00:00"}'
    )

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return BrokerResponse(status=200, body=self.body)


@pytest.mark.asyncio
async def test_issue_kis_access_token_has_no_contract_reader_dependency() -> None:
    transport = TokenTransport(
        body=(
            b'{"access_token":"token",'
            b'"access_token_token_expired":"2026-08-11 09:00:00"}'
        )
    )

    token = await issue_kis_access_token(
        transport=transport,
        credentials=KisClientCredentials(app_key="app", app_secret="secret"),
    )

    assert token.value == "token"
    assert transport.requests[0].path == "/oauth2/tokenP"


@dataclass
class ContinuationTransport:
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return BrokerResponse(
            status=200,
            body=b'{"output":[]}',
            headers=(("tr_cont", "M"),),
        )


@dataclass
class PagedPositionTransport:
    responses: list[BrokerResponse]
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return self.responses.pop(0)


@dataclass
class ContractReader:
    contract: KisActiveContract

    async def load_active(
        self, *, evidence_id: UUID, now: datetime
    ) -> KisActiveContract:
        assert evidence_id == self.contract.evidence_id
        assert now < self.contract.expires_at
        return self.contract


def credentials() -> KisReadCredentials:
    return KisReadCredentials(access_token="token", app_key="app", app_secret="secret")


def active_contract(**overrides: object) -> KisActiveContract:
    values: dict[str, object] = {
        "evidence_id": uuid7(),
        "data_source_id": uuid7(),
        "instrument_id": uuid7(),
        "provider_contract_code": "NQZ26",
        "provider_exchange_code": "CME",
        "expires_at": datetime(2026, 8, 10, 1, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return KisActiveContract(**values)  # type: ignore[arg-type]


def test_kis_minute_page_keeps_utc_and_bar_volume_unassigned() -> None:
    page = decode_kis_minute_chart_page(
        BrokerResponse(
            status=200,
            body=(
                b'{"output1":{"ret_cnt":"2","index_key":"next-page"},'
                b'"output2":[{"data_date":"20260810","data_time":"090500",'
                b'"open_price":"100","high_price":"103","low_price":"99",'
                b'"last_price":"102","vol":"1200"},{"data_date":"20260810",'
                b'"data_time":"091000","open_price":"102","high_price":"104",'
                b'"low_price":"101","last_price":"103","vol":"1500"}]}'
            ),
        )
    )

    assert page == KisMinuteChartPage(
        records=(
            KisMinuteChartRecord(
                trading_date=date(2026, 8, 10),
                trading_time=time(9, 5),
                open_price=Decimal("100"),
                high_price=Decimal("103"),
                low_price=Decimal("99"),
                close_price=Decimal("102"),
                cumulative_volume=Decimal("1200"),
            ),
            KisMinuteChartRecord(
                trading_date=date(2026, 8, 10),
                trading_time=time(9, 10),
                open_price=Decimal("102"),
                high_price=Decimal("104"),
                low_price=Decimal("101"),
                close_price=Decimal("103"),
                cumulative_volume=Decimal("1500"),
            ),
        ),
        continuation_key="next-page",
    )


def test_kis_contract_detail_page_binds_an_explicit_provider_code_to_raw_body() -> None:
    response = BrokerResponse(
        status=200,
        body=b'{"rt_cd":"0","output2":[{"srs_cd":"NQZ26"}]}',
    )

    page = decode_kis_contract_detail_page(response)

    assert page.require_provider_contract_code("NQZ26") == "NQZ26"
    assert page.canonical_payload_hash == sha256(response.body).digest()
    with pytest.raises(ValueError, match="not present"):
        page.require_provider_contract_code("MNQZ26")


def test_kis_contract_detail_page_rejects_direct_forged_construction() -> None:
    with pytest.raises(TypeError, match="decoder"):
        KisContractDetailPage(
            provider_contract_codes=("NQZ26",),
            canonical_payload_hash=b"x" * 32,
        )


def test_kis_minute_page_rejects_a_provider_record_count_mismatch() -> None:
    with pytest.raises(ValueError, match="record count"):
        decode_kis_minute_chart_page(
            BrokerResponse(
                status=200,
                body=(
                    b'{"output1":{"ret_cnt":"2","index_key":""},'
                    b'"output2":[{"data_date":"20260810","data_time":"090500",'
                    b'"open_price":"100","high_price":"103","low_price":"99",'
                    b'"last_price":"102","vol":"1200"}]}'
                ),
            )
        )


def test_kis_futures_price_page_decodes_provider_local_quote_fields() -> None:
    page = decode_kis_futures_price_page(
        BrokerResponse(
            status=200,
            body=(
                b'{"output1":{"proc_date":"20260810","proc_time":"090500",'
                b'"open_price":"100","high_price":"103","low_price":"99",'
                b'"last_price":"102","vol":"1200","exch_cd":"CME",'
                b'"crc_cd":"USD"}}'
            ),
        )
    )

    assert page == KisFuturesPricePage(
        records=(
            KisFuturesPriceRecord(
                provider_date=date(2026, 8, 10),
                provider_time=time(9, 5),
                open_price=Decimal("100"),
                high_price=Decimal("103"),
                low_price=Decimal("99"),
                last_price=Decimal("102"),
                cumulative_volume=Decimal("1200"),
                exchange_code="CME",
                currency_code="USD",
            ),
        )
    )


def test_kis_futures_price_page_accepts_provider_output_list() -> None:
    page = decode_kis_futures_price_page(
        BrokerResponse(
            status=200,
            body=(
                b'{"output1":[{"proc_date":"20260810","proc_time":"090500",'
                b'"open_price":"100","high_price":"103","low_price":"99",'
                b'"last_price":"102","vol":"1200","exch_cd":"CME",'
                b'"crc_cd":"USD"}]}'
            ),
        )
    )

    assert len(page.records) == 1


@pytest.mark.asyncio
async def test_kis_rejects_a_reader_contract_for_the_wrong_instrument() -> None:
    contract = active_contract()
    transport = RecordingTransport()
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    with pytest.raises(UnsupportedBrokerInstrument, match="instrument"):
        await adapter.read_price(
            evidence_id=contract.evidence_id,
            instrument_id=uuid7(),
            now=datetime(2026, 8, 10, tzinfo=UTC),
            credentials=credentials(),
        )

    assert transport.requests == []


@pytest.mark.asyncio
async def test_kis_active_persisted_contract_allows_only_a_quote_request() -> None:
    contract = active_contract()
    transport = RecordingTransport()
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    await adapter.read_price(
        evidence_id=contract.evidence_id,
        instrument_id=contract.instrument_id,
        now=datetime(2026, 8, 10, tzinfo=UTC),
        credentials=credentials(),
    )

    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path="/uapi/overseas-futureoption/v1/quotations/inquire-price?SRS_CD=NQZ26",
            headers=(
                ("appkey", "app"),
                ("appsecret", "secret"),
                ("authorization", "Bearer token"),
                ("custtype", "P"),
                ("tr_id", "HHDFC55010000"),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_kis_reads_one_minute_chart_page_from_exact_active_evidence() -> None:
    contract = active_contract()
    transport = RecordingTransport()
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    await adapter.read_minute_chart(
        evidence_id=contract.evidence_id,
        instrument_id=contract.instrument_id,
        now=datetime(2026, 8, 10, tzinfo=UTC),
        credentials=credentials(),
        start_date=date(2026, 8, 9),
        close_date=date(2026, 8, 10),
        interval=KisMinuteChartInterval.FIVE_MINUTES,
        count=120,
    )

    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path=(
                "/uapi/overseas-futureoption/v1/quotations/"
                "inquire-time-futurechartprice?SRS_CD=NQZ26&EXCH_CD=CME&"
                "START_DATE_TIME=20260809&CLOSE_DATE_TIME=20260810&"
                "QRY_TP=Q&QRY_CNT=120&QRY_GAP=5&INDEX_KEY="
            ),
            headers=(
                ("appkey", "app"),
                ("appsecret", "secret"),
                ("authorization", "Bearer token"),
                ("custtype", "P"),
                ("tr_id", "HHDFC55020400"),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_kis_reads_only_an_explicit_minute_chart_continuation_page() -> None:
    contract = active_contract()
    transport = RecordingTransport()
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    await adapter.read_next_minute_chart_page(
        evidence_id=contract.evidence_id,
        instrument_id=contract.instrument_id,
        now=datetime(2026, 8, 10, tzinfo=UTC),
        credentials=credentials(),
        start_date=date(2026, 8, 9),
        close_date=date(2026, 8, 10),
        interval=KisMinuteChartInterval.FIVE_MINUTES,
        count=120,
        index_key="next-page-key",
    )

    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path=(
                "/uapi/overseas-futureoption/v1/quotations/"
                "inquire-time-futurechartprice?SRS_CD=NQZ26&EXCH_CD=CME&"
                "START_DATE_TIME=20260809&CLOSE_DATE_TIME=20260810&"
                "QRY_TP=P&QRY_CNT=120&QRY_GAP=5&INDEX_KEY=next-page-key"
            ),
            headers=(
                ("appkey", "app"),
                ("appsecret", "secret"),
                ("authorization", "Bearer token"),
                ("custtype", "P"),
                ("tr_id", "HHDFC55020400"),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_kis_collects_bounded_complete_minute_chart_pages() -> None:
    contract = active_contract()
    transport = PagedPositionTransport(
        responses=[
            BrokerResponse(
                status=200,
                body=(
                    b'{"output1":{"ret_cnt":"1","index_key":"next"},'
                    b'"output2":[{"data_date":"20260810","data_time":"090000",'
                    b'"open_price":"1","high_price":"1","low_price":"1",'
                    b'"last_price":"1","vol":"1"}]}'
                ),
            ),
            BrokerResponse(
                status=200,
                body=(
                    b'{"output1":{"ret_cnt":"1","index_key":""},'
                    b'"output2":[{"data_date":"20260810","data_time":"085500",'
                    b'"open_price":"1","high_price":"1","low_price":"1",'
                    b'"last_price":"1","vol":"2"}]}'
                ),
            ),
        ]
    )
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    pages = await adapter.read_complete_minute_chart(
        evidence_id=contract.evidence_id,
        instrument_id=contract.instrument_id,
        now=datetime(2026, 8, 10, tzinfo=UTC),
        credentials=credentials(),
        start_date=date(2026, 8, 10),
        close_date=date(2026, 8, 10),
        interval=KisMinuteChartInterval.FIVE_MINUTES,
        count=120,
        max_pages=2,
    )

    assert len(pages) == 2
    assert "QRY_TP=Q" in transport.requests[0].path
    assert "QRY_TP=P" in transport.requests[1].path
    assert "INDEX_KEY=next" in transport.requests[1].path


@pytest.mark.asyncio
async def test_kis_rejects_minute_chart_that_exceeds_page_limit() -> None:
    contract = active_contract()
    transport = PagedPositionTransport(
        responses=[
            BrokerResponse(
                status=200,
                body=(b'{"output1":{"ret_cnt":"0","index_key":"next"},"output2":[]}'),
            )
        ]
    )
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    with pytest.raises(KisIncompleteMinuteChartSnapshot, match="page limit"):
        await adapter.read_complete_minute_chart(
            evidence_id=contract.evidence_id,
            instrument_id=contract.instrument_id,
            now=datetime(2026, 8, 10, tzinfo=UTC),
            credentials=credentials(),
            start_date=date(2026, 8, 10),
            close_date=date(2026, 8, 10),
            interval=KisMinuteChartInterval.ONE_MINUTE,
            count=1,
            max_pages=1,
        )

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_kis_rejects_a_blank_minute_chart_continuation_key() -> None:
    contract = active_contract()
    transport = RecordingTransport()
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    with pytest.raises(ValueError, match="index key"):
        await adapter.read_next_minute_chart_page(
            evidence_id=contract.evidence_id,
            instrument_id=contract.instrument_id,
            now=datetime(2026, 8, 10, tzinfo=UTC),
            credentials=credentials(),
            start_date=date(2026, 8, 9),
            close_date=date(2026, 8, 10),
            interval=KisMinuteChartInterval.FIVE_MINUTES,
            count=120,
            index_key="",
        )

    assert transport.requests == []


@pytest.mark.asyncio
async def test_kis_rejects_minute_chart_with_nonpositive_count_before_transport() -> (
    None
):
    contract = active_contract()
    transport = RecordingTransport()
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    with pytest.raises(ValueError, match="count"):
        await adapter.read_minute_chart(
            evidence_id=contract.evidence_id,
            instrument_id=contract.instrument_id,
            now=datetime(2026, 8, 10, tzinfo=UTC),
            credentials=credentials(),
            start_date=date(2026, 8, 10),
            close_date=date(2026, 8, 10),
            interval=KisMinuteChartInterval.ONE_MINUTE,
            count=0,
        )

    assert transport.requests == []


@pytest.mark.asyncio
async def test_kis_auth_exchanges_call_scoped_client_credentials() -> None:
    contract = active_contract()
    transport = TokenTransport()
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    token = await adapter.issue_access_token(
        credentials=KisClientCredentials(app_key="app", app_secret="secret")
    )

    assert token == KisAccessToken(
        value="issued-token",
        expires_at_raw="2026-08-11 09:00:00",
    )
    assert transport.requests == [
        BrokerRequest(
            method="POST",
            path="/oauth2/tokenP",
            headers=(
                ("Accept", "text/plain"),
                ("Content-Type", "application/json; charset=UTF-8"),
            ),
            body=b'{"grant_type":"client_credentials","appkey":"app","appsecret":"secret"}',
        )
    ]


@pytest.mark.asyncio
async def test_kis_auth_rejects_an_unparseable_provider_expiry() -> None:
    contract = active_contract()
    adapter = KisReadOnlyAdapter(
        transport=TokenTransport(
            body=(
                b'{"access_token":"issued-token",'
                b'"access_token_token_expired":"not-a-timestamp"}'
            )
        ),
        contract_reader=ContractReader(contract),
    )

    with pytest.raises(KisAuthenticationError, match="invalid"):
        await adapter.issue_access_token(
            credentials=KisClientCredentials(app_key="app", app_secret="secret")
        )


@pytest.mark.asyncio
async def test_kis_auth_rejects_a_noncanonical_provider_expiry() -> None:
    contract = active_contract()
    adapter = KisReadOnlyAdapter(
        transport=TokenTransport(
            body=(
                b'{"access_token":"issued-token",'
                b'"access_token_token_expired":"2026-8-1 9:00:00"}'
            )
        ),
        contract_reader=ContractReader(contract),
    )

    with pytest.raises(KisAuthenticationError, match="invalid"):
        await adapter.issue_access_token(
            credentials=KisClientCredentials(app_key="app", app_secret="secret")
        )


@pytest.mark.asyncio
async def test_kis_reads_an_overseas_futures_open_position_snapshot() -> None:
    contract = active_contract()
    transport = RecordingTransport()
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    await adapter.read_open_positions(
        credentials=KisAccountReadCredentials(
            access_token="token",
            app_key="app",
            app_secret="secret",
            account_number="81012345",
            product_code="08",
        ),
        futures_option_division="01",
    )

    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path=(
                "/uapi/overseas-futureoption/v1/trading/inquire-unpd?"
                "CANO=81012345&ACNT_PRDT_CD=08&FUOP_DVSN=01&"
                "CTX_AREA_FK100=&CTX_AREA_NK100="
            ),
            headers=(
                ("appkey", "app"),
                ("appsecret", "secret"),
                ("authorization", "Bearer token"),
                ("custtype", "P"),
                ("tr_id", "OTFM1412R"),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_kis_collects_contract_detail_pages_for_explicit_provider_codes() -> None:
    contract = active_contract()
    first = BrokerResponse(
        status=200,
        body=b'{"rt_cd":"0","output2":[{"srs_cd":"NQZ26"}]}',
        headers=(("tr_cont", "F"),),
    )
    final = BrokerResponse(
        status=200,
        body=b'{"rt_cd":"0","output2":[{"srs_cd":"MNQZ26"}]}',
    )
    transport = PagedPositionTransport(responses=[first, final])
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    pages = await adapter.read_complete_contract_details(
        credentials=credentials(),
        provider_contract_codes=("NQZ26", "MNQZ26"),
        max_pages=2,
    )

    expected_query = urlencode(
        [("QRY_CNT", "2")]
        + [
            (f"SRS_CD_{index:02d}", code)
            for index, code in enumerate(("NQZ26", "MNQZ26"), start=1)
        ]
        + [(f"SRS_CD_{index:02d}", "") for index in range(3, 33)]
    )
    assert pages == (first, final)
    assert transport.requests[0] == BrokerRequest(
        method="GET",
        path=(
            "/uapi/overseas-futureoption/v1/quotations/"
            f"search-contract-detail?{expected_query}"
        ),
        headers=(
            ("appkey", "app"),
            ("appsecret", "secret"),
            ("authorization", "Bearer token"),
            ("custtype", "P"),
            ("tr_id", "HHDFC55200000"),
        ),
    )
    assert ("tr_cont", "N") in transport.requests[1].headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b'{"rt_cd":"1","msg1":"provider error"}',
        b'{"rt_cd":"0","output2":{"srs_cd":"NQZ26"}}',
    ],
)
async def test_kis_rejects_an_invalid_contract_detail_success_envelope(
    body: bytes,
) -> None:
    contract = active_contract()
    transport = PagedPositionTransport(
        responses=[BrokerResponse(status=200, body=body)]
    )
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    with pytest.raises(KisIncompleteContractSnapshot, match="success envelope"):
        await adapter.read_complete_contract_details(
            credentials=credentials(),
            provider_contract_codes=("NQZ26",),
            max_pages=1,
        )

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_kis_rejects_a_non_futures_account_before_transport() -> None:
    contract = active_contract()
    transport = RecordingTransport()
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    with pytest.raises(ValueError, match="product code"):
        await adapter.read_open_positions(
            credentials=KisAccountReadCredentials(
                access_token="token",
                app_key="app",
                app_secret="secret",
                account_number="81012345",
                product_code="01",
            ),
            futures_option_division="01",
        )

    assert transport.requests == []


@pytest.mark.asyncio
async def test_kis_rejects_a_continued_position_snapshot_before_reconciliation() -> (
    None
):
    contract = active_contract()
    transport = ContinuationTransport()
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    with pytest.raises(KisIncompleteAccountSnapshot, match="continuation"):
        await adapter.read_open_positions(
            credentials=KisAccountReadCredentials(
                access_token="token",
                app_key="app",
                app_secret="secret",
                account_number="81012345",
                product_code="08",
            ),
            futures_option_division="01",
        )

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_kis_rejects_an_unsuccessful_single_position_snapshot() -> None:
    contract = active_contract()
    transport = PagedPositionTransport(
        responses=[BrokerResponse(status=503, body=b'{"msg1":"unavailable"}')]
    )
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    with pytest.raises(KisIncompleteAccountSnapshot, match="not successful"):
        await adapter.read_open_positions(
            credentials=KisAccountReadCredentials(
                access_token="token",
                app_key="app",
                app_secret="secret",
                account_number="81012345",
                product_code="08",
            ),
            futures_option_division="01",
        )

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_kis_collects_all_provider_directed_position_pages() -> None:
    contract = active_contract()
    first = BrokerResponse(
        status=200,
        body=b'{"output":[{"page":1}]}',
        headers=(("tr_cont", "M"),),
    )
    final = BrokerResponse(status=200, body=b'{"output":[{"page":2}]}')
    transport = PagedPositionTransport(responses=[first, final])
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )
    credentials = KisAccountReadCredentials(
        access_token="token",
        app_key="app",
        app_secret="secret",
        account_number="81012345",
        product_code="08",
    )

    pages = await adapter.read_complete_open_positions(
        credentials=credentials,
        futures_option_division="01",
        max_pages=2,
    )

    assert pages == (first, final)
    assert transport.requests[0].headers == (
        ("appkey", "app"),
        ("appsecret", "secret"),
        ("authorization", "Bearer token"),
        ("custtype", "P"),
        ("tr_id", "OTFM1412R"),
    )
    assert ("tr_cont", "N") in transport.requests[1].headers


@pytest.mark.asyncio
async def test_kis_rejects_unfinished_position_pages_at_the_declared_limit() -> None:
    contract = active_contract()
    page = BrokerResponse(
        status=200,
        body=b'{"output":[{"page":1}]}',
        headers=(("tr_cont", "M"),),
    )
    transport = PagedPositionTransport(responses=[page])
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    with pytest.raises(KisIncompleteAccountSnapshot, match="page limit"):
        await adapter.read_complete_open_positions(
            credentials=KisAccountReadCredentials(
                access_token="token",
                app_key="app",
                app_secret="secret",
                account_number="81012345",
                product_code="08",
            ),
            futures_option_division="01",
            max_pages=1,
        )

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_kis_collects_all_provider_directed_daily_order_pages() -> None:
    contract = active_contract()
    first = BrokerResponse(
        status=200,
        body=b'{"output":[{"page":1}]}',
        headers=(("tr_cont", "M"),),
    )
    final = BrokerResponse(status=200, body=b'{"output":[{"page":2}]}')
    transport = PagedPositionTransport(responses=[first, final])
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )
    credentials = KisAccountReadCredentials(
        access_token="token",
        app_key="app",
        app_secret="secret",
        account_number="81012345",
        product_code="08",
    )

    pages = await adapter.read_complete_daily_orders(
        credentials=credentials,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 10),
        execution_status="03",
        side="%%",
        futures_option_division="01",
        max_pages=2,
    )

    assert pages == (first, final)
    assert transport.requests[0] == BrokerRequest(
        method="GET",
        path=(
            "/uapi/overseas-futureoption/v1/trading/inquire-daily-order?"
            "CANO=81012345&ACNT_PRDT_CD=08&STRT_DT=20260801&END_DT=20260810&"
            "FM_PDGR_CD=&CCLD_NCCS_DVSN=03&SLL_BUY_DVSN_CD=%25%25&"
            "FUOP_DVSN=01&CTX_AREA_FK200=&CTX_AREA_NK200="
        ),
        headers=(
            ("appkey", "app"),
            ("appsecret", "secret"),
            ("authorization", "Bearer token"),
            ("custtype", "P"),
            ("tr_id", "OTFM3120R"),
        ),
    )
    assert ("tr_cont", "N") in transport.requests[1].headers


@pytest.mark.asyncio
async def test_kis_rejects_unfinished_daily_order_pages_at_the_declared_limit() -> None:
    contract = active_contract()
    page = BrokerResponse(
        status=200,
        body=b'{"output":[{"page":1}]}',
        headers=(("tr_cont", "M"),),
    )
    transport = PagedPositionTransport(responses=[page])
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    with pytest.raises(KisIncompleteAccountSnapshot, match="page limit"):
        await adapter.read_complete_daily_orders(
            credentials=KisAccountReadCredentials(
                access_token="token",
                app_key="app",
                app_secret="secret",
                account_number="81012345",
                product_code="08",
            ),
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 10),
            execution_status="03",
            side="%%",
            futures_option_division="01",
            max_pages=1,
        )

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_kis_rejects_an_error_page_during_complete_position_collection() -> None:
    contract = active_contract()
    transport = PagedPositionTransport(
        responses=[
            BrokerResponse(
                status=200,
                body=b'{"output":[]}',
                headers=(("tr_cont", "M"),),
            ),
            BrokerResponse(status=503, body=b'{"msg1":"unavailable"}'),
        ]
    )
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    with pytest.raises(KisIncompleteAccountSnapshot, match="not successful"):
        await adapter.read_complete_open_positions(
            credentials=KisAccountReadCredentials(
                access_token="token",
                app_key="app",
                app_secret="secret",
                account_number="81012345",
                product_code="08",
            ),
            futures_option_division="01",
            max_pages=2,
        )

    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_kis_rejects_an_error_page_during_complete_daily_order_collection() -> (
    None
):
    contract = active_contract()
    transport = PagedPositionTransport(
        responses=[
            BrokerResponse(
                status=200,
                body=b'{"output":[]}',
                headers=(("tr_cont", "M"),),
            ),
            BrokerResponse(status=503, body=b'{"msg1":"unavailable"}'),
        ]
    )
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    with pytest.raises(KisIncompleteAccountSnapshot, match="not successful"):
        await adapter.read_complete_daily_orders(
            credentials=KisAccountReadCredentials(
                access_token="token",
                app_key="app",
                app_secret="secret",
                account_number="81012345",
                product_code="08",
            ),
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 10),
            execution_status="03",
            side="%%",
            futures_option_division="01",
            max_pages=2,
        )

    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_kis_rejects_invalid_daily_order_filters_before_transport() -> None:
    contract = active_contract()
    transport = RecordingTransport()
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    with pytest.raises(ValueError, match="execution status"):
        await adapter.read_complete_daily_orders(
            credentials=KisAccountReadCredentials(
                access_token="token",
                app_key="app",
                app_secret="secret",
                account_number="81012345",
                product_code="08",
            ),
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 10),
            execution_status="00",
            side="%%",
            futures_option_division="01",
            max_pages=1,
        )

    assert transport.requests == []


@pytest.mark.asyncio
async def test_kis_writes_are_blocked_before_transport() -> None:
    contract = active_contract()
    transport = RecordingTransport()
    adapter = KisReadOnlyAdapter(
        transport=transport,
        contract_reader=ContractReader(contract),
    )

    with pytest.raises(BrokerWriteDisabled):
        await adapter.cancel(command=object())

    assert transport.requests == []
