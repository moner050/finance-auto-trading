from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from autotrader.integrations.brokers.common import (
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)
from autotrader.integrations.brokers.kis.adapter import KisReadCredentials
from autotrader.integrations.brokers.kis.domestic_cash import (
    KisDomesticCashAccount,
    KisDomesticCashBalanceSnapshot,
    KisDomesticCashBuyingPower,
    KisDomesticCashHolding,
    KisDomesticCashReadOnlyAdapter,
    KisIncompleteDomesticCashSnapshot,
    decode_kis_domestic_cash_balance_page,
    decode_kis_domestic_cash_buying_power,
)

_SENTINELS = (
    "SENTINEL_ACCOUNT",
    "SENTINEL_SYMBOL",
    "SENTINEL_AMOUNT",
    "SENTINEL_CURSOR",
    "SENTINEL_BODY",
)


@dataclass
class RecordingTransport:
    responses: list[BrokerResponse]
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def credentials() -> KisReadCredentials:
    return KisReadCredentials(access_token="token", app_key="app", app_secret="secret")


def account() -> KisDomesticCashAccount:
    return KisDomesticCashAccount(account_number="81012345", product_code="01")


def success_balance(*, holdings: bytes = b"[]") -> BrokerResponse:
    return BrokerResponse(status=200, body=b'{"rt_cd":"0","output1":' + holdings + b"}")


@pytest.mark.asyncio
async def test_balance_reads_all_pages_with_documented_continuation_request() -> None:
    transport = RecordingTransport(
        responses=[
            BrokerResponse(
                status=200,
                headers=(("tr_cont", "M"),),
                body=(
                    b'{"rt_cd":"0","ctx_area_fk100":"next-fk",'
                    b'"ctx_area_nk100":"next-nk","output1":['
                    b'{"pdno":"005930","hldg_qty":"3"}]}'
                ),
            ),
            success_balance(holdings=b'[{"pdno":"000660","hldg_qty":"2"}]'),
        ]
    )

    snapshot = await KisDomesticCashReadOnlyAdapter(
        transport=transport
    ).read_complete_balance(credentials=credentials(), account=account(), max_pages=2)

    assert snapshot == KisDomesticCashBalanceSnapshot(
        holdings=(
            KisDomesticCashHolding(symbol="005930", quantity=Decimal("3")),
            KisDomesticCashHolding(symbol="000660", quantity=Decimal("2")),
        )
    )
    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path=(
                "/uapi/domestic-stock/v1/trading/inquire-balance?"
                "CANO=81012345&ACNT_PRDT_CD=01&AFHR_FLPR_YN=N&"
                "OFL_YN=&INQR_DVSN=02&UNPR_DVSN=01&FUND_STTL_ICLD_YN=N&"
                "FNCG_AMT_AUTO_RDPT_YN=N&PRCS_DVSN=00&CTX_AREA_FK100=&CTX_AREA_NK100="
            ),
            headers=(
                ("authorization", "Bearer token"),
                ("appkey", "app"),
                ("appsecret", "secret"),
                ("custtype", "P"),
                ("tr_id", "TTTC8434R"),
            ),
        ),
        BrokerRequest(
            method="GET",
            path=(
                "/uapi/domestic-stock/v1/trading/inquire-balance?"
                "CANO=81012345&ACNT_PRDT_CD=01&AFHR_FLPR_YN=N&"
                "OFL_YN=&INQR_DVSN=02&UNPR_DVSN=01&FUND_STTL_ICLD_YN=N&"
                "FNCG_AMT_AUTO_RDPT_YN=N&PRCS_DVSN=00&"
                "CTX_AREA_FK100=next-fk&CTX_AREA_NK100=next-nk"
            ),
            headers=(
                ("authorization", "Bearer token"),
                ("appkey", "app"),
                ("appsecret", "secret"),
                ("custtype", "P"),
                ("tr_cont", "N"),
                ("tr_id", "TTTC8434R"),
            ),
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "max_pages"),
    [
        (BrokerResponse(status=500, body=b"{}"), 2),
        (
            BrokerResponse(
                status=200,
                headers=(("tr_cont", "M"),),
                body=b'{"rt_cd":"0","output1":[]}',
            ),
            2,
        ),
        (
            BrokerResponse(
                status=200,
                headers=(("tr_cont", "M"),),
                body=b'{"rt_cd":"0","ctx_area_fk100":"a","ctx_area_nk100":"b","output1":[]}',
            ),
            1,
        ),
    ],
)
async def test_balance_rejects_incomplete_snapshots(
    response: BrokerResponse, max_pages: int
) -> None:
    adapter = KisDomesticCashReadOnlyAdapter(
        transport=RecordingTransport(responses=[response])
    )

    with pytest.raises(KisIncompleteDomesticCashSnapshot) as raised:
        await adapter.read_complete_balance(
            credentials=credentials(), account=account(), max_pages=max_pages
        )
    assert raised.value.args == ("KIS domestic cash snapshot is incomplete",)
    assert str(raised.value) == "KIS domestic cash snapshot is incomplete"


@pytest.mark.asyncio
async def test_balance_rejects_a_repeated_continuation_cursor() -> None:
    page = BrokerResponse(
        status=200,
        headers=(("tr_cont", "M"),),
        body=b'{"rt_cd":"0","ctx_area_fk100":"a","ctx_area_nk100":"b","output1":[]}',
    )
    adapter = KisDomesticCashReadOnlyAdapter(
        transport=RecordingTransport(responses=[page, page])
    )

    with pytest.raises(KisIncompleteDomesticCashSnapshot) as raised:
        await adapter.read_complete_balance(
            credentials=credentials(), account=account(), max_pages=3
        )
    assert raised.value.args == (
        "KIS domestic cash snapshot has a repeated continuation",
    )
    assert str(raised.value) == "KIS domestic cash snapshot has a repeated continuation"


@pytest.mark.asyncio
async def test_buying_power_uses_only_documented_read_request() -> None:
    transport = RecordingTransport(
        responses=[
            BrokerResponse(
                status=200,
                body=b'{"rt_cd":"0","output":{"nrcvb_buy_amt":"210000","nrcvb_buy_qty":"3"}}',
            )
        ]
    )

    result = await KisDomesticCashReadOnlyAdapter(
        transport=transport
    ).read_buying_power(
        credentials=credentials(),
        account=account(),
        symbol="005930",
        reference_price=Decimal("70000"),
    )

    assert result == KisDomesticCashBuyingPower(
        amount=Decimal("210000"), quantity=Decimal("3")
    )
    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path=(
                "/uapi/domestic-stock/v1/trading/inquire-psbl-order?"
                "CANO=81012345&ACNT_PRDT_CD=01&PDNO=005930&ORD_UNPR=70000&"
                "ORD_DVSN=01&CMA_EVLU_AMT_ICLD_YN=N&OVRS_ICLD_YN=N"
            ),
            headers=(
                ("authorization", "Bearer token"),
                ("appkey", "app"),
                ("appsecret", "secret"),
                ("custtype", "P"),
                ("tr_id", "TTTC8908R"),
            ),
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_price", [Decimal("7E+4"), Decimal("70000.0")])
async def test_buying_power_canonicalizes_integral_decimal_reference_prices(
    reference_price: Decimal,
) -> None:
    transport = RecordingTransport(
        responses=[
            BrokerResponse(
                status=200,
                body=b'{"rt_cd":"0","output":{"nrcvb_buy_amt":"210000","nrcvb_buy_qty":"3"}}',
            )
        ]
    )

    await KisDomesticCashReadOnlyAdapter(transport=transport).read_buying_power(
        credentials=credentials(),
        account=account(),
        symbol="005930",
        reference_price=reference_price,
    )

    assert "ORD_UNPR=70000" in transport.requests[0].path


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b'{"rt_cd":"1"}',
        b'{"rt_cd":"0","output1":[{"pdno":"005930","hldg_qty":"1.1"}]}',
        b'{"rt_cd":"0","output":{"nrcvb_buy_amt":"-1","nrcvb_buy_qty":"3"}}',
        b'{"rt_cd":"0","output":{"nrcvb_buy_amt":"1","nrcvb_buy_qty":"x"}}',
        b'{"rt_cd":"0","output":{"nrcvb_buy_amt":"1234567890123456789012345678901","nrcvb_buy_qty":"3"}}',
    ],
)
def test_cash_decoders_reject_malformed_or_non_integral_values(raw: bytes) -> None:
    with pytest.raises(ValueError) as balance_raised:
        decode_kis_domestic_cash_balance_page(BrokerResponse(status=200, body=raw))
    assert balance_raised.value.args == ("KIS domestic cash response is invalid",)
    assert str(balance_raised.value) == "KIS domestic cash response is invalid"
    with pytest.raises(ValueError) as buying_power_raised:
        decode_kis_domestic_cash_buying_power(BrokerResponse(status=200, body=raw))
    assert buying_power_raised.value.args == ("KIS domestic cash response is invalid",)
    assert str(buying_power_raised.value) == "KIS domestic cash response is invalid"


def test_cash_decoder_errors_do_not_retain_raw_provider_values() -> None:
    body = (
        b'{"rt_cd":"0","output1":[{"pdno":"SENTINEL_SYMBOL",'
        b'"hldg_qty":"SENTINEL_AMOUNT","account":"SENTINEL_ACCOUNT"}]}'
    )
    with pytest.raises(ValueError) as raised:
        decode_kis_domestic_cash_balance_page(BrokerResponse(status=200, body=body))
    del body
    error = raised.value
    assert error.args == ("KIS domestic cash response is invalid",)
    assert str(error) == "KIS domestic cash response is invalid"
    assert error.__cause__ is None and error.__context__ is None
    assert all(
        value not in str(error) and value not in error.args
        for value in ("SENTINEL_SYMBOL", "SENTINEL_AMOUNT", "SENTINEL_ACCOUNT")
    )
    for frame, _ in traceback.walk_tb(error.__traceback__):
        assert all(
            value not in repr(local)
            for local in frame.f_locals.values()
            for value in ("SENTINEL_SYMBOL", "SENTINEL_AMOUNT", "SENTINEL_ACCOUNT")
        )


@pytest.mark.asyncio
async def test_adapter_errors_do_not_retain_balance_response_values() -> None:
    error = await _malformed_balance_error()

    _assert_scrubbed_error(error, expected="KIS domestic cash snapshot is incomplete")


@pytest.mark.asyncio
async def test_adapter_errors_do_not_retain_continuation_values() -> None:
    error = await _continuation_balance_error()

    _assert_scrubbed_error(error, expected="KIS domestic cash snapshot is incomplete")


@pytest.mark.asyncio
async def test_write_methods_are_blocked_before_transport_requests() -> None:
    transport = RecordingTransport(responses=[])
    adapter = KisDomesticCashReadOnlyAdapter(transport=transport)
    for method in (adapter.submit, adapter.cancel, adapter.replace):
        with pytest.raises(BrokerWriteDisabled) as raised:
            await method(command=object())
        assert raised.value.args == ("KIS domestic cash write adapter is not enabled",)
        assert str(raised.value) == "KIS domestic cash write adapter is not enabled"
    assert transport.requests == []


async def _malformed_balance_error() -> RuntimeError:
    body = (
        b'{"rt_cd":"0","output1":[{"pdno":"SENTINEL_SYMBOL",'
        b'"hldg_qty":"SENTINEL_AMOUNT","account":"SENTINEL_ACCOUNT",'
        b'"body":"SENTINEL_BODY"}]}'
    )
    transport = RecordingTransport(responses=[BrokerResponse(status=200, body=body)])
    adapter = KisDomesticCashReadOnlyAdapter(transport=transport)
    account_value = KisDomesticCashAccount(account_number="81012345", product_code="01")
    try:
        await adapter.read_complete_balance(
            credentials=credentials(), account=account_value, max_pages=1
        )
    except KisIncompleteDomesticCashSnapshot as error:
        del body, transport, adapter, account_value
        return error
    raise AssertionError("expected an incomplete snapshot")


async def _continuation_balance_error() -> RuntimeError:
    body = (
        b'{"rt_cd":"0","ctx_area_fk100":"SENTINEL_CURSOR",'
        b'"ctx_area_nk100":"SENTINEL_CURSOR","output1":[]}'
    )
    transport = RecordingTransport(
        responses=[BrokerResponse(status=200, headers=(("tr_cont", "M"),), body=body)]
    )
    adapter = KisDomesticCashReadOnlyAdapter(transport=transport)
    account_value = KisDomesticCashAccount(account_number="81012345", product_code="01")
    try:
        await adapter.read_complete_balance(
            credentials=credentials(), account=account_value, max_pages=1
        )
    except KisIncompleteDomesticCashSnapshot as error:
        del body, transport, adapter, account_value
        return error
    raise AssertionError("expected an incomplete snapshot")


def _assert_scrubbed_error(error: BaseException, *, expected: str) -> None:
    assert error.args == (expected,)
    assert str(error) == expected
    assert error.__cause__ is None and error.__context__ is None
    for frame, _ in traceback.walk_tb(error.__traceback__):
        assert all(
            sentinel not in repr(local)
            for local in frame.f_locals.values()
            for sentinel in _SENTINELS
        )
