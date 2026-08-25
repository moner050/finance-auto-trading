from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import cast

import pytest

from autotrader.integrations.brokers.common import (
    BrokerMarket,
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
    UnsupportedBrokerMarket,
)
from autotrader.integrations.brokers.toss.adapter import (
    TossAccessToken,
    TossAccount,
    TossCandleInterval,
    TossCandlePage,
    TossCandleRecord,
    TossClientCredentials,
    TossIncompleteCandleSnapshot,
    TossKrwCashBuyingPower,
    TossPricePage,
    TossPriceRecord,
    TossReadOnlyAdapter,
    decode_toss_accounts,
    decode_toss_candle_page,
    decode_toss_krw_cash_buying_power,
    decode_toss_krx_cash_holding_presence,
    decode_toss_price_page,
    select_single_brokerage_account,
)
from autotrader.integrations.brokers.toss.market_data_reader import (
    TossAccessToken as NeutralTossAccessToken,
)
from autotrader.integrations.brokers.toss.market_data_reader import (
    TossClientCredentials as NeutralTossClientCredentials,
)


def test_legacy_oauth_types_reexport_neutral_market_data_types() -> None:
    assert TossAccessToken is NeutralTossAccessToken
    assert TossClientCredentials is NeutralTossClientCredentials


@dataclass
class RecordingTransport:
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return BrokerResponse(status=200, body=b"{}")


@dataclass
class TokenTransport:
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return BrokerResponse(
            status=200,
            body=b'{"access_token":"issued-token","token_type":"Bearer","expires_in":86400}',
        )


@dataclass
class PagedOrderTransport:
    responses: list[BrokerResponse]
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return self.responses.pop(0)


@dataclass
class PagedCandleTransport:
    responses: list[BrokerResponse]
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def candle_response(timestamp: str, *, next_before: str | None) -> BrokerResponse:
    next_before_json = "null" if next_before is None else f'"{next_before}"'
    return BrokerResponse(
        status=200,
        body=(
            '{"result":{"candles":[{"timestamp":"'
            + timestamp
            + '","openPrice":"72000","highPrice":"72100",'
            '"lowPrice":"71950","closePrice":"72050","volume":"15200",'
            + '"currency":"KRW"}],"nextBefore":'
            + next_before_json
            + "}}"
        ).encode(),
    )


@pytest.mark.asyncio
async def test_toss_rejects_nq_before_transport() -> None:
    transport = RecordingTransport()
    adapter = TossReadOnlyAdapter(transport=transport)

    with pytest.raises(UnsupportedBrokerMarket, match="OVERSEAS_FUTURES"):
        await adapter.read_price(
            market=BrokerMarket.OVERSEAS_FUTURES,
            symbol="NQ",
            access_token="token",
        )

    assert transport.requests == []


@pytest.mark.asyncio
async def test_toss_price_keeps_the_token_out_of_the_path() -> None:
    transport = RecordingTransport()
    adapter = TossReadOnlyAdapter(transport=transport)

    await adapter.read_price(
        market=BrokerMarket.US_STOCK,
        symbol="AAPL",
        access_token="market-token",
    )
    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path="/api/v1/prices?symbols=AAPL",
            headers=(("Authorization", "Bearer market-token"),),
        ),
    ]


@pytest.mark.asyncio
async def test_toss_reads_bounded_recent_trades_without_account_scope() -> None:
    transport = RecordingTransport()
    adapter = TossReadOnlyAdapter(transport=transport)

    await adapter.read_recent_trades(
        market=BrokerMarket.KRX_STOCK,
        symbol="005930",
        count=50,
        access_token="market-token",
    )

    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path="/api/v1/trades?symbol=005930&count=50",
            headers=(("Authorization", "Bearer market-token"),),
        )
    ]


@pytest.mark.asyncio
async def test_toss_reads_orderbook_without_account_scope() -> None:
    transport = RecordingTransport()
    adapter = TossReadOnlyAdapter(transport=transport)

    await adapter.read_orderbook(
        market=BrokerMarket.US_STOCK,
        symbol="AAPL",
        access_token="market-token",
    )

    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path="/api/v1/orderbook?symbol=AAPL",
            headers=(("Authorization", "Bearer market-token"),),
        )
    ]


@pytest.mark.asyncio
async def test_toss_stock_candles_encode_the_pagination_cursor() -> None:
    transport = RecordingTransport()
    adapter = TossReadOnlyAdapter(transport=transport)

    await adapter.read_candles(
        market=BrokerMarket.US_STOCK,
        symbol="AAPL",
        interval=TossCandleInterval.ONE_MINUTE,
        count=200,
        before=datetime(2026, 8, 10, 9, 0, tzinfo=timezone(timedelta(hours=9))),
        adjusted=False,
        access_token="market-token",
    )

    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path=(
                "/api/v1/candles?symbol=AAPL&interval=1m&count=200&"
                "before=2026-08-10T09%3A00%3A00%2B09%3A00&adjusted=false"
            ),
            headers=(("Authorization", "Bearer market-token"),),
        )
    ]


@pytest.mark.asyncio
async def test_toss_stock_candles_preserve_a_provider_pagination_cursor() -> None:
    transport = RecordingTransport()
    adapter = TossReadOnlyAdapter(transport=transport)

    await adapter.read_candles(
        market=BrokerMarket.KRX_STOCK,
        symbol="005930",
        interval=TossCandleInterval.ONE_MINUTE,
        count=1,
        before="2026-03-25T09:32:00+09:00",
        adjusted=True,
        access_token="market-token",
    )

    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path=(
                "/api/v1/candles?symbol=005930&interval=1m&count=1&"
                "before=2026-03-25T09%3A32%3A00%2B09%3A00&adjusted=true"
            ),
            headers=(("Authorization", "Bearer market-token"),),
        )
    ]


@pytest.mark.asyncio
async def test_toss_krx_market_data_never_requires_an_account_header() -> None:
    transport = RecordingTransport()
    adapter = TossReadOnlyAdapter(transport=transport)

    await adapter.read_candles(
        market=BrokerMarket.KRX_STOCK,
        symbol="005930",
        interval=TossCandleInterval.ONE_MINUTE,
        count=30,
        before=None,
        adjusted=True,
        access_token="market-token",
    )

    assert all(
        key.lower() != "x-tossinvest-account"
        for key, _ in transport.requests[0].headers
    )


@pytest.mark.asyncio
async def test_toss_collects_candle_pages_with_the_inclusive_next_before_cursor() -> (
    None
):
    transport = PagedCandleTransport(
        responses=[
            candle_response(
                "2026-08-11T09:31:00+09:00",
                next_before="2026-08-11T09:31:00+09:00",
            ),
            candle_response("2026-08-11T09:30:00+09:00", next_before=None),
        ]
    )

    pages = await TossReadOnlyAdapter(transport=transport).read_complete_candle_pages(
        market=BrokerMarket.KRX_STOCK,
        symbol="005930",
        interval=TossCandleInterval.ONE_MINUTE,
        count=200,
        before=None,
        adjusted=True,
        access_token="market-token",
        max_pages=2,
    )

    assert len(pages) == 2
    assert [request.path for request in transport.requests] == [
        "/api/v1/candles?symbol=005930&interval=1m&count=200&adjusted=true",
        "/api/v1/candles?symbol=005930&interval=1m&count=200&before="
        "2026-08-11T09%3A31%3A00%2B09%3A00&adjusted=true",
    ]
    assert all(
        key.lower() != "x-tossinvest-account"
        for request in transport.requests
        for key, _ in request.headers
    )


@pytest.mark.asyncio
async def test_toss_reads_a_daily_candle_tail_until_provider_termination() -> None:
    transport = PagedCandleTransport(
        responses=[
            candle_response(
                "2026-08-10T00:00:00+00:00",
                next_before="2026-08-10T00:00:00+00:00",
            ),
            candle_response("2026-08-09T00:00:00+00:00", next_before=None),
        ]
    )
    adapter = TossReadOnlyAdapter(transport=transport)

    pages = await adapter.read_recent_candle_pages(
        market=BrokerMarket.KRX_STOCK,
        symbol="005930",
        interval=TossCandleInterval.ONE_DAY,
        count=200,
        before="2026-08-11T00:00:00+00:00",
        adjusted=True,
        access_token="market-token",
        max_pages=2,
    )

    assert len(pages) == 2
    assert "%2B00%3A00" in transport.requests[1].path
    assert "symbols=" not in transport.requests[0].path


@pytest.mark.asyncio
async def test_toss_returns_a_valid_recent_candle_tail_at_its_page_cap() -> None:
    transport = PagedCandleTransport(
        responses=[
            candle_response(
                "2026-08-10T00:00:00+00:00",
                next_before="2026-08-10T00:00:00+00:00",
            ),
            candle_response(
                "2026-08-09T00:00:00+00:00",
                next_before="2026-08-09T00:00:00+00:00",
            ),
        ]
    )

    pages = await TossReadOnlyAdapter(transport=transport).read_recent_candle_pages(
        market=BrokerMarket.KRX_STOCK,
        symbol="005930",
        interval=TossCandleInterval.ONE_DAY,
        count=200,
        before="2026-08-11T00:00:00+00:00",
        adjusted=True,
        access_token="market-token",
        max_pages=2,
    )

    assert len(pages) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    (
        BrokerResponse(
            status=200,
            body=b'{"result":{"candles":[],"nextBefore":"2026-08-10T00:00:00+00:00"}}',
        ),
        candle_response(
            "2026-08-11T00:00:00+00:00",
            next_before="2026-08-11T00:00:00+00:00",
        ),
    ),
)
async def test_toss_rejects_an_invalid_capped_recent_candle_continuation(
    response: BrokerResponse,
) -> None:
    transport = PagedCandleTransport(responses=[response])

    with pytest.raises(TossIncompleteCandleSnapshot, match="did not advance"):
        await TossReadOnlyAdapter(transport=transport).read_recent_candle_pages(
            market=BrokerMarket.KRX_STOCK,
            symbol="005930",
            interval=TossCandleInterval.ONE_DAY,
            count=200,
            before="2026-08-11T00:00:00+00:00",
            adjusted=True,
            access_token="market-token",
            max_pages=1,
        )

    assert len(transport.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("max_pages", (0, True, False))
async def test_toss_recent_candle_tail_rejects_an_invalid_page_limit_before_transport(
    max_pages: object,
) -> None:
    transport = PagedCandleTransport(responses=[])

    with pytest.raises(ValueError, match="page limit"):
        await TossReadOnlyAdapter(transport=transport).read_recent_candle_pages(
            market=BrokerMarket.KRX_STOCK,
            symbol="005930",
            interval=TossCandleInterval.ONE_DAY,
            count=1,
            before=None,
            adjusted=True,
            access_token="market-token",
            max_pages=max_pages,
        )

    assert transport.requests == []


@pytest.mark.asyncio
async def test_toss_candle_collection_fails_closed_on_non_advancing_cursor() -> None:
    transport = PagedCandleTransport(
        responses=[
            candle_response(
                "2026-08-11T09:31:00+09:00",
                next_before="2026-08-11T09:30:00+09:00",
            ),
            candle_response(
                "2026-08-11T09:30:00+09:00",
                next_before="2026-08-11T09:30:00+09:00",
            ),
        ]
    )

    with pytest.raises(TossIncompleteCandleSnapshot, match="did not advance"):
        await TossReadOnlyAdapter(transport=transport).read_complete_candle_pages(
            market=BrokerMarket.KRX_STOCK,
            symbol="005930",
            interval=TossCandleInterval.ONE_MINUTE,
            count=200,
            before=None,
            adjusted=True,
            access_token="market-token",
            max_pages=2,
        )


@pytest.mark.asyncio
async def test_toss_candle_collection_fails_closed_at_page_limit() -> None:
    transport = PagedCandleTransport(
        responses=[
            candle_response(
                "2026-08-11T09:31:00+09:00",
                next_before="2026-08-11T09:31:00+09:00",
            )
        ]
    )

    with pytest.raises(TossIncompleteCandleSnapshot, match="exceeded"):
        await TossReadOnlyAdapter(transport=transport).read_complete_candle_pages(
            market=BrokerMarket.KRX_STOCK,
            symbol="005930",
            interval=TossCandleInterval.ONE_MINUTE,
            count=200,
            before=None,
            adjusted=True,
            access_token="market-token",
            max_pages=1,
        )


@pytest.mark.asyncio
async def test_toss_candle_collection_fails_closed_on_empty_continuation_page() -> None:
    transport = PagedCandleTransport(
        responses=[
            BrokerResponse(
                status=200,
                body=b'{"result":{"candles":[],"nextBefore":"2026-08-11T09:31:00+09:00"}}',
            )
        ]
    )

    with pytest.raises(TossIncompleteCandleSnapshot, match="did not advance"):
        await TossReadOnlyAdapter(transport=transport).read_complete_candle_pages(
            market=BrokerMarket.KRX_STOCK,
            symbol="005930",
            interval=TossCandleInterval.ONE_MINUTE,
            count=1,
            before=None,
            adjusted=True,
            access_token="market-token",
            max_pages=2,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_page", "message"),
    [
        (BrokerResponse(status=200, body=b"not-json"), "not valid JSON"),
        (BrokerResponse(status=500, body=b"{}"), "not successful"),
    ],
)
async def test_toss_candle_collection_rejects_partial_snapshot_on_invalid_later_page(
    invalid_page: BrokerResponse,
    message: str,
) -> None:
    transport = PagedCandleTransport(
        responses=[
            candle_response(
                "2026-08-11T09:31:00+09:00",
                next_before="2026-08-11T09:31:00+09:00",
            ),
            invalid_page,
        ]
    )

    with pytest.raises(ValueError, match=message):
        await TossReadOnlyAdapter(transport=transport).read_complete_candle_pages(
            market=BrokerMarket.KRX_STOCK,
            symbol="005930",
            interval=TossCandleInterval.ONE_MINUTE,
            count=200,
            before=None,
            adjusted=True,
            access_token="market-token",
            max_pages=2,
        )

    assert len(transport.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("max_pages", [0, 11, True, "1"])
async def test_toss_candle_collection_rejects_an_invalid_page_limit(
    max_pages: object,
) -> None:
    transport = PagedCandleTransport(responses=[])

    with pytest.raises(ValueError, match="page limit"):
        await TossReadOnlyAdapter(transport=transport).read_complete_candle_pages(
            market=BrokerMarket.KRX_STOCK,
            symbol="005930",
            interval=TossCandleInterval.ONE_MINUTE,
            count=1,
            before=None,
            adjusted=True,
            access_token="market-token",
            max_pages=max_pages,
        )

    assert transport.requests == []


@pytest.mark.asyncio
async def test_toss_auth_exchanges_call_scoped_client_credentials() -> None:
    transport = TokenTransport()
    adapter = TossReadOnlyAdapter(transport=transport)

    token = await adapter.issue_access_token(
        credentials=TossClientCredentials(
            client_id="client-id",
            client_secret="client-secret",
        )
    )

    assert token == TossAccessToken(value="issued-token", expires_in_seconds=86400)
    assert transport.requests == [
        BrokerRequest(
            method="POST",
            path="/oauth2/token",
            headers=(("Content-Type", "application/x-www-form-urlencoded"),),
            body=b"grant_type=client_credentials&client_id=client-id&client_secret=client-secret",
        )
    ]


@pytest.mark.asyncio
async def test_toss_reads_account_scoped_reconciliation_observations() -> None:
    transport = RecordingTransport()
    adapter = TossReadOnlyAdapter(transport=transport)

    await adapter.read_accounts(access_token="account-token")
    await adapter.read_holdings(access_token="account-token", account_seq=17)
    await adapter.read_holdings(
        access_token="account-token", account_seq=17, symbol="005930"
    )
    await adapter.read_orders(
        access_token="account-token",
        account_seq=17,
        status="OPEN",
        symbol="005930",
    )

    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path="/api/v1/accounts",
            headers=(("Authorization", "Bearer account-token"),),
        ),
        BrokerRequest(
            method="GET",
            path="/api/v1/holdings",
            headers=(
                ("Authorization", "Bearer account-token"),
                ("X-Tossinvest-Account", "17"),
            ),
        ),
        BrokerRequest(
            method="GET",
            path="/api/v1/holdings?symbol=005930",
            headers=(
                ("Authorization", "Bearer account-token"),
                ("X-Tossinvest-Account", "17"),
            ),
        ),
        BrokerRequest(
            method="GET",
            path="/api/v1/orders?status=OPEN&symbol=005930",
            headers=(
                ("Authorization", "Bearer account-token"),
                ("X-Tossinvest-Account", "17"),
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_toss_reads_krw_cash_buying_power_for_selected_account() -> None:
    transport = RecordingTransport()
    adapter = TossReadOnlyAdapter(transport=transport)

    await adapter.read_krw_cash_buying_power(
        access_token="account-token",
        account=TossAccount(account_seq=17, account_type="BROKERAGE"),
    )

    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path="/api/v1/buying-power?currency=KRW",
            headers=(
                ("Authorization", "Bearer account-token"),
                ("X-Tossinvest-Account", "17"),
            ),
        )
    ]


def test_toss_decodes_nonnegative_krw_cash_buying_power() -> None:
    snapshot = decode_toss_krw_cash_buying_power(
        BrokerResponse(
            status=200,
            body=b'{"result":{"currency":"KRW","cashBuyingPower":"5000000"}}',
        )
    )

    assert snapshot == TossKrwCashBuyingPower(amount=Decimal("5000000"))


def test_toss_decodes_matching_krx_cash_holding_presence() -> None:
    assert decode_toss_krx_cash_holding_presence(
        BrokerResponse(
            status=200,
            body=(
                b'{"result":{"items":[{"symbol":"005930",'
                b'"marketCountry":"KR","currency":"KRW","quantity":"1"}]}}'
            ),
        ),
        symbol="005930",
    )


def test_toss_decodes_empty_krx_cash_holding_presence_as_absent() -> None:
    assert not decode_toss_krx_cash_holding_presence(
        BrokerResponse(status=200, body=b'{"result":{"items":[]}}'),
        symbol="005930",
    )


@pytest.mark.parametrize(
    "response",
    (
        BrokerResponse(
            status=200,
            body=(
                b'{"result":{"items":[{"symbol":"005930",'
                b'"marketCountry":"KR","currency":"KRW","quantity":"1"},'
                b'{"symbol":"005930","marketCountry":"KR",'
                b'"currency":"KRW","quantity":"1"}]}}'
            ),
        ),
        BrokerResponse(
            status=200,
            body=(
                b'{"result":{"items":[{"symbol":"000660",'
                b'"marketCountry":"KR","currency":"KRW","quantity":"1"}]}}'
            ),
        ),
        BrokerResponse(
            status=200,
            body=(
                b'{"result":{"items":[{"symbol":"005930",'
                b'"marketCountry":"US","currency":"KRW","quantity":"1"}]}}'
            ),
        ),
        BrokerResponse(
            status=200,
            body=(
                b'{"result":{"items":[{"symbol":"005930",'
                b'"marketCountry":"KR","currency":"USD","quantity":"1"}]}}'
            ),
        ),
        BrokerResponse(
            status=200,
            body=(
                b'{"result":{"items":[{"symbol":"005930",'
                b'"marketCountry":"KR","currency":"KRW","quantity":"0"}]}}'
            ),
        ),
        BrokerResponse(
            status=200,
            body=(
                b'{"result":{"items":[{"symbol":"005930",'
                b'"marketCountry":"KR","currency":"KRW","quantity":"0.5"}]}}'
            ),
        ),
        BrokerResponse(
            status=200,
            body=(
                b'{"result":{"items":[{"symbol":"005930",'
                b'"marketCountry":"KR","currency":"KRW","quantity":"'
                + (b"1" * 31)
                + b'"}]}}'
            ),
        ),
        BrokerResponse(status=200, body=b'{"result":{}}'),
        BrokerResponse(status=500, body=b"{}"),
        BrokerResponse(status=200, body=b"not-json"),
    ),
)
def test_toss_krx_cash_holding_presence_decoder_rejects_invalid_snapshots(
    response: BrokerResponse,
) -> None:
    with pytest.raises(ValueError, match="Toss KRX cash holding"):
        decode_toss_krx_cash_holding_presence(response, symbol="005930")


def _malformed_toss_krx_cash_holding_decode_error() -> ValueError:
    with pytest.raises(ValueError) as error:
        decode_toss_krx_cash_holding_presence(
            BrokerResponse(
                status=200,
                body=(
                    b'{"result":{"items":[{"accountNo":"12345678901",'
                    b'"name":"private-name","symbol":"005930",'
                    b'"marketCountry":"KR","currency":"KRW",'
                    b'"quantity":"17","amount":"5000000"}]}'
                ),
            ),
            symbol="005930",
        )

    return error.value


def test_toss_krx_cash_holding_decoder_does_not_retain_malformed_response_data() -> (
    None
):
    raised = _malformed_toss_krx_cash_holding_decode_error()

    assert raised.__cause__ is None
    assert raised.__context__ is None
    for private_value in ("12345678901", "private-name", "17", "5000000"):
        assert private_value not in str(raised)
        assert private_value not in repr(raised.args)
        assert all(
            private_value not in repr(value)
            for value in _traceback_frame_local_values(raised)
        )


def _invalid_symbol_toss_krx_cash_holding_decode_error() -> ValueError:
    with pytest.raises(ValueError) as error:
        decode_toss_krx_cash_holding_presence(
            BrokerResponse(
                status=200,
                body=b'{"marker":"synthetic-invalid-symbol-response-marker"}',
            ),
            symbol="invalid symbol",
        )

    return error.value


def test_toss_krx_cash_holding_decoder_discards_response_before_symbol_error() -> None:
    marker = "synthetic-invalid-symbol-response-marker"
    raised = _invalid_symbol_toss_krx_cash_holding_decode_error()

    assert str(raised) == "Toss KRX cash holding symbol is invalid"
    assert marker not in str(raised)
    assert marker not in repr(raised.args)
    assert raised.__cause__ is None
    assert raised.__context__ is None
    assert all(
        marker not in repr(value) for value in _traceback_frame_local_values(raised)
    )


def _recursive_toss_krx_cash_holding_response_body() -> bytes:
    return (
        b'{"result":'
        + (b'{"nested":' * 10_000)
        + b'{"accountNo":"12345678901","name":"private-name",'
        b'"quantity":"17","amount":"5000000"}' + (b"}" * 10_000) + b"}"
    )


def _recursive_toss_krx_cash_holding_decode_error() -> ValueError:
    with pytest.raises(ValueError) as error:
        decode_toss_krx_cash_holding_presence(
            BrokerResponse(
                status=200, body=_recursive_toss_krx_cash_holding_response_body()
            ),
            symbol="005930",
        )

    return error.value


def test_toss_krx_cash_holding_decoder_does_not_retain_recursive_response_data() -> (
    None
):
    raised = _recursive_toss_krx_cash_holding_decode_error()
    assert raised.__cause__ is None
    assert raised.__context__ is None
    for private_value in ("12345678901", "private-name", "17", "5000000"):
        assert private_value not in str(raised)
        assert private_value not in repr(raised.args)
        assert all(
            private_value not in repr(value)
            for value in _traceback_frame_local_values(raised)
        )


def _over_limit_numeric_toss_krx_cash_holding_response_body() -> bytes:
    return (
        b'{"result":{"items":[{"accountNo":"12345678901",'
        b'"name":"private-name","quantity":"17","amount":"5000000",'
        b'"providerNumericLiteral":' + (b"1" * 4_301) + b"}]}}"
    )


def _over_limit_numeric_toss_krx_cash_holding_decode_error() -> ValueError:
    with pytest.raises(ValueError) as error:
        decode_toss_krx_cash_holding_presence(
            BrokerResponse(
                status=200,
                body=_over_limit_numeric_toss_krx_cash_holding_response_body(),
            ),
            symbol="005930",
        )

    return error.value


def test_toss_krx_cash_holding_decoder_does_not_retain_parser_failure_data() -> None:
    raised = _over_limit_numeric_toss_krx_cash_holding_decode_error()

    assert raised.__cause__ is None
    assert raised.__context__ is None
    for private_value in ("12345678901", "private-name", "17", "5000000"):
        assert private_value not in str(raised)
        assert private_value not in repr(raised.args)
        assert all(
            private_value not in repr(value)
            for value in _traceback_frame_local_values(raised)
        )


@pytest.mark.parametrize(
    ("cash_buying_power", "expected_amount"),
    (
        ("0", Decimal()),
        ("999999999999999999999999999999", Decimal("999999999999999999999999999999")),
    ),
)
def test_toss_decodes_zero_and_max_length_krw_cash_buying_power(
    cash_buying_power: str, expected_amount: Decimal
) -> None:
    snapshot = decode_toss_krw_cash_buying_power(
        BrokerResponse(
            status=200,
            body=(
                b'{"result":{"currency":"KRW","cashBuyingPower":"'
                + cash_buying_power.encode()
                + b'"}}'
            ),
        )
    )

    assert snapshot == TossKrwCashBuyingPower(amount=expected_amount)


@pytest.mark.parametrize("cash_buying_power", ("0.5", "1" * 31))
def test_toss_cash_buying_power_decoder_rejects_fractional_or_oversized_amount(
    cash_buying_power: str,
) -> None:
    with pytest.raises(ValueError, match="Toss KRW cash buying power") as error:
        decode_toss_krw_cash_buying_power(
            BrokerResponse(
                status=200,
                body=(
                    b'{"result":{"currency":"KRW","cashBuyingPower":"'
                    + cash_buying_power.encode()
                    + b'"}}'
                ),
            )
        )

    assert cash_buying_power not in str(error.value)
    assert cash_buying_power not in repr(error.value.args)


@pytest.mark.parametrize("amount", (Decimal("0.5"), Decimal("1" * 31)))
def test_toss_krw_cash_buying_power_rejects_fractional_or_oversized_amount(
    amount: Decimal,
) -> None:
    with pytest.raises(ValueError, match="Toss KRW cash buying power"):
        TossKrwCashBuyingPower(amount=amount)


@pytest.mark.parametrize(
    "response",
    (
        BrokerResponse(status=500, body=b"{}"),
        BrokerResponse(status=200, body=b"not-json"),
        BrokerResponse(status=200, body=b'{"result":[]}'),
        BrokerResponse(
            status=200,
            body=b'{"result":{"currency":"USD","cashBuyingPower":"10"}}',
        ),
        BrokerResponse(
            status=200,
            body=b'{"result":{"currency":"KRW","cashBuyingPower":"-1"}}',
        ),
    ),
)
def test_toss_cash_buying_power_decoder_rejects_invalid_snapshots(
    response: BrokerResponse,
) -> None:
    with pytest.raises(ValueError, match="Toss KRW cash buying power"):
        decode_toss_krw_cash_buying_power(response)


def _malformed_toss_cash_buying_power_decode_error() -> ValueError:
    with pytest.raises(ValueError) as error:
        decode_toss_krw_cash_buying_power(
            BrokerResponse(
                status=200,
                body=(
                    b'{"result":{"accountNo":"12345678901",'
                    b'"currency":"KRW","cashBuyingPower":"5000000"}'
                ),
            )
        )

    return error.value


def test_toss_cash_buying_power_decoder_does_not_retain_malformed_response_data() -> (
    None
):
    account_number = "12345678901"
    amount = "5000000"
    raised = _malformed_toss_cash_buying_power_decode_error()

    assert raised.__cause__ is None
    assert raised.__context__ is None
    assert account_number not in str(raised)
    assert amount not in str(raised)
    assert account_number not in repr(raised.args)
    assert amount not in repr(raised.args)
    assert all(
        account_number not in repr(value) and amount not in repr(value)
        for value in _traceback_frame_local_values(raised)
    )


def _recursive_toss_cash_buying_power_response_body() -> bytes:
    return (
        b'{"result":'
        + (b'{"nested":' * 10_000)
        + b'{"accountNo":"12345678901","currency":"KRW",'
        b'"cashBuyingPower":"5000000"}' + (b"}" * 10_000) + b"}"
    )


def _recursive_toss_cash_buying_power_decode_error() -> ValueError:
    with pytest.raises(ValueError) as error:
        decode_toss_krw_cash_buying_power(
            BrokerResponse(
                status=200,
                body=_recursive_toss_cash_buying_power_response_body(),
            )
        )

    return error.value


def test_toss_cash_buying_power_decoder_does_not_retain_recursive_response_data() -> (
    None
):
    account_number = "12345678901"
    amount = "5000000"
    raised = _recursive_toss_cash_buying_power_decode_error()

    assert raised.__cause__ is None
    assert raised.__context__ is None
    assert account_number not in str(raised)
    assert amount not in str(raised)
    assert account_number not in repr(raised.args)
    assert amount not in repr(raised.args)
    assert all(
        account_number not in repr(value) and amount not in repr(value)
        for value in _traceback_frame_local_values(raised)
    )


@pytest.mark.asyncio
async def test_toss_rejects_non_account_cash_buying_power_scope_before_transport() -> (
    None
):
    transport = RecordingTransport()
    adapter = TossReadOnlyAdapter(transport=transport)

    with pytest.raises(ValueError, match="requires a TossAccount"):
        await adapter.read_krw_cash_buying_power(
            access_token="account-token",
            account=cast(TossAccount, object()),
        )

    assert transport.requests == []


def test_toss_decodes_a_brokerage_account_without_retaining_account_number() -> None:
    accounts = decode_toss_accounts(
        BrokerResponse(
            status=200,
            body=(
                b'{"result":[{"accountNo":"12345678901",'
                b'"accountSeq":17,"accountType":"BROKERAGE"}]}'
            ),
        )
    )

    assert accounts == (TossAccount(account_seq=17, account_type="BROKERAGE"),)
    assert not hasattr(accounts[0], "account_no")
    assert select_single_brokerage_account(accounts) is accounts[0]


@pytest.mark.parametrize(
    ("response", "message"),
    (
        (BrokerResponse(status=500, body=b"{}"), "not successful"),
        (BrokerResponse(status=200, body=b"not-json"), "not valid JSON"),
        (BrokerResponse(status=200, body=b'{"result":{}}'), "result is invalid"),
        (
            BrokerResponse(
                status=200,
                body=(
                    b'{"result":[{"accountNo":"","accountSeq":17,'
                    b'"accountType":"BROKERAGE"}]}'
                ),
            ),
            "record is invalid",
        ),
        (
            BrokerResponse(
                status=200,
                body=(
                    b'{"result":[{"accountNo":"123","accountSeq":true,'
                    b'"accountType":"BROKERAGE"}]}'
                ),
            ),
            "account sequence",
        ),
    ),
)
def test_toss_account_decoder_rejects_invalid_provider_snapshots(
    response: BrokerResponse, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        decode_toss_accounts(response)


def _malformed_toss_account_decode_error() -> ValueError:
    with pytest.raises(ValueError) as error:
        decode_toss_accounts(
            BrokerResponse(
                status=200,
                body=(
                    b'{"result":[{"accountNo":"12345678901",'
                    b'"accountSeq":17,"accountType":"BROKERAGE"}'
                ),
            )
        )

    return error.value


def _traceback_frame_local_values(error: BaseException) -> list[object]:
    values: list[object] = []
    traceback = error.__traceback__
    while traceback is not None:
        values.extend(traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    return values


def test_toss_account_decoder_does_not_retain_malformed_response_identifiers() -> None:
    account_number = "12345678901"
    raised = _malformed_toss_account_decode_error()

    assert raised.__cause__ is None
    assert raised.__context__ is None
    assert account_number not in str(raised)
    assert account_number not in repr(raised.args)
    assert all(
        account_number not in repr(value)
        for value in _traceback_frame_local_values(raised)
    )


@pytest.mark.parametrize(
    "accounts",
    (
        (),
        (
            TossAccount(account_seq=17, account_type="BROKERAGE"),
            TossAccount(account_seq=18, account_type="BROKERAGE"),
        ),
        (TossAccount(account_seq=17, account_type="PENSION_SAVINGS"),),
        [TossAccount(account_seq=17, account_type="BROKERAGE")],
    ),
)
def test_toss_account_selector_rejects_ambiguous_or_mutable_scope(
    accounts: object,
) -> None:
    with pytest.raises(ValueError) as error:
        select_single_brokerage_account(accounts)

    assert "17" not in str(error.value)
    assert "12345678901" not in str(error.value)


@pytest.mark.asyncio
async def test_toss_reads_an_individual_order_with_an_encoded_opaque_id() -> None:
    transport = RecordingTransport()
    adapter = TossReadOnlyAdapter(transport=transport)

    await adapter.read_order_detail(
        access_token="account-token",
        account_seq=17,
        order_id="issued/order id",
    )

    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path="/api/v1/orders/issued%2Forder%20id",
            headers=(
                ("Authorization", "Bearer account-token"),
                ("X-Tossinvest-Account", "17"),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_toss_rejects_a_nonpositive_account_sequence_before_transport() -> None:
    transport = RecordingTransport()
    adapter = TossReadOnlyAdapter(transport=transport)

    with pytest.raises(ValueError, match="account sequence"):
        await adapter.read_holdings(access_token="account-token", account_seq=0)

    assert transport.requests == []


@pytest.mark.asyncio
async def test_toss_collects_all_closed_order_history_pages() -> None:
    first = BrokerResponse(
        status=200,
        body=b'{"result":{"orders":[],"nextCursor":"next-page","hasNext":true}}',
    )
    final = BrokerResponse(
        status=200,
        body=b'{"result":{"orders":[],"nextCursor":null,"hasNext":false}}',
    )
    transport = PagedOrderTransport(responses=[first, final])
    adapter = TossReadOnlyAdapter(transport=transport)

    pages = await adapter.read_complete_closed_orders(
        access_token="account-token",
        account_seq=17,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 10),
        symbol="005930",
        max_pages=2,
    )

    assert pages == (first, final)
    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path=(
                "/api/v1/orders?status=CLOSED&symbol=005930&from=2026-08-01&"
                "to=2026-08-10&limit=100"
            ),
            headers=(
                ("Authorization", "Bearer account-token"),
                ("X-Tossinvest-Account", "17"),
            ),
        ),
        BrokerRequest(
            method="GET",
            path=(
                "/api/v1/orders?status=CLOSED&symbol=005930&from=2026-08-01&"
                "to=2026-08-10&limit=100&cursor=next-page"
            ),
            headers=(
                ("Authorization", "Bearer account-token"),
                ("X-Tossinvest-Account", "17"),
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_toss_rejects_unfinished_closed_order_history_at_page_limit() -> None:
    page = BrokerResponse(
        status=200,
        body=b'{"result":{"orders":[],"nextCursor":"next-page","hasNext":true}}',
    )
    transport = PagedOrderTransport(responses=[page])
    adapter = TossReadOnlyAdapter(transport=transport)

    with pytest.raises(RuntimeError, match="page limit"):
        await adapter.read_complete_closed_orders(
            access_token="account-token",
            account_seq=17,
            start_date=None,
            end_date=None,
            symbol=None,
            max_pages=1,
        )

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_toss_writes_are_blocked_before_transport() -> None:
    transport = RecordingTransport()
    adapter = TossReadOnlyAdapter(transport=transport)

    with pytest.raises(BrokerWriteDisabled):
        await adapter.submit(command=object())

    assert transport.requests == []


def test_toss_candle_page_decodes_provider_start_timestamps_and_cursor() -> None:
    page = decode_toss_candle_page(
        BrokerResponse(
            status=200,
            body=(
                b'{"result":{"candles":[{"timestamp":"2026-03-25T09:32:00+09:00",'
                b'"openPrice":"72000","highPrice":"72100","lowPrice":"71950",'
                b'"closePrice":"72050","volume":"15200","currency":"KRW"}],'
                b'"nextBefore":"2026-03-25T09:32:00+09:00"}}'
            ),
        )
    )

    assert page == TossCandlePage(
        records=(
            TossCandleRecord(
                timestamp=datetime(
                    2026, 3, 25, 9, 32, tzinfo=timezone(timedelta(hours=9))
                ),
                open_price=Decimal("72000"),
                high_price=Decimal("72100"),
                low_price=Decimal("71950"),
                close_price=Decimal("72050"),
                volume=Decimal("15200"),
                currency="KRW",
            ),
        ),
        next_before="2026-03-25T09:32:00+09:00",
    )


def test_toss_candle_page_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="Toss candle timestamp is invalid"):
        decode_toss_candle_page(
            BrokerResponse(
                status=200,
                body=(
                    b'{"result":{"candles":[{"timestamp":"2026-03-25T09:32:00",'
                    b'"openPrice":"72000","highPrice":"72100","lowPrice":"71950",'
                    b'"closePrice":"72050","volume":"15200","currency":"KRW"}],'
                    b'"nextBefore":null}}'
                ),
            )
        )


def test_toss_price_page_decodes_documented_current_price_fields() -> None:
    page = decode_toss_price_page(
        BrokerResponse(
            status=200,
            body=(
                b'{"result":[{"symbol":"AAPL",'
                b'"timestamp":"2026-03-25T22:30:00.456+09:00",'
                b'"lastPrice":"185.70","currency":"USD"}]}'
            ),
        )
    )

    assert page == TossPricePage(
        records=(
            TossPriceRecord(
                symbol="AAPL",
                timestamp=datetime(
                    2026,
                    3,
                    25,
                    22,
                    30,
                    0,
                    456000,
                    tzinfo=timezone(timedelta(hours=9)),
                ),
                last_price=Decimal("185.70"),
                currency="USD",
            ),
        )
    )


def test_toss_price_page_preserves_an_absent_provider_timestamp() -> None:
    page = decode_toss_price_page(
        BrokerResponse(
            status=200,
            body=(
                b'{"result":[{"symbol":"005930","timestamp":null,'
                b'"lastPrice":"72000","currency":"KRW"}]}'
            ),
        )
    )

    assert page.records[0].timestamp is None
