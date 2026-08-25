from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from autotrader.integrations.brokers.common import (
    BrokerMarket,
    BrokerRequest,
    BrokerResponse,
)
from autotrader.integrations.brokers.toss.market_data_contracts import (
    TossCandleInterval,
)
from autotrader.integrations.brokers.toss.market_data_reader import (
    TossIncompleteCandleSnapshot,
    TossMarketDataReadOnlyAdapter,
)


@dataclass
class _SinglePageTransport:
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return BrokerResponse(
            status=200,
            body=b'{"result":{"candles":[],"nextBefore":null}}',
        )


@dataclass
class _PagedTransport:
    responses: list[BrokerResponse]
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def _candle_page_response(*, timestamp: str, next_before: str | None) -> BrokerResponse:
    next_before_value = "null" if next_before is None else f'"{next_before}"'
    return BrokerResponse(
        status=200,
        body=(
            '{"result":{"candles":[{'
            f'"timestamp":"{timestamp}",'
            '"openPrice":"100","highPrice":"102",'
            '"lowPrice":"99","closePrice":"101",'
            '"volume":"7","currency":"KRW"'
            f'}}],"nextBefore":{next_before_value}}}}}'
        ).encode(),
    )


@pytest.mark.asyncio
async def test_toss_candle_reader_uses_canonical_singular_symbol_query() -> None:
    transport = _SinglePageTransport()

    await TossMarketDataReadOnlyAdapter(transport=transport).read_complete_candle_pages(
        market=BrokerMarket.KRX_STOCK,
        symbol="005930",
        interval=TossCandleInterval.ONE_MINUTE,
        count=200,
        before=datetime(2026, 8, 12, 0, 0, tzinfo=UTC),
        adjusted=True,
        access_token="market-token",
        max_pages=1,
    )

    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path=(
                "/api/v1/candles?symbol=005930&interval=1m&count=200&"
                "before=2026-08-12T00%3A00%3A00Z&adjusted=true"
            ),
            headers=(("Authorization", "Bearer market-token"),),
        )
    ]


@pytest.mark.asyncio
async def test_toss_candle_tail_stops_at_a_valid_page_cap() -> None:
    observed_at = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
    responses = [
        _candle_page_response(
            timestamp="2026-08-11T23:59:00+00:00",
            next_before="2026-08-11T23:59:00+00:00",
        ),
        _candle_page_response(
            timestamp="2026-08-11T23:58:00+00:00",
            next_before="2026-08-11T23:58:00+00:00",
        ),
    ]
    transport = _PagedTransport(responses=responses.copy())

    pages = await TossMarketDataReadOnlyAdapter(
        transport=transport
    ).read_recent_candle_pages(
        market=BrokerMarket.KRX_STOCK,
        symbol="005930",
        interval=TossCandleInterval.ONE_MINUTE,
        count=200,
        before=observed_at,
        adjusted=True,
        access_token="market-token",
        max_pages=2,
    )

    assert len(pages) == 2
    assert [request.path for request in transport.requests] == [
        (
            "/api/v1/candles?symbol=005930&interval=1m&count=200&"
            "before=2026-08-12T00%3A00%3A00Z&adjusted=true"
        ),
        (
            "/api/v1/candles?symbol=005930&interval=1m&count=200&"
            "before=2026-08-11T23%3A59%3A00%2B00%3A00&adjusted=true"
        ),
    ]
    assert "before=2026-08-11T23%3A59%3A00Z" not in transport.requests[1].path

    with pytest.raises(TossIncompleteCandleSnapshot, match="exceeded"):
        await TossMarketDataReadOnlyAdapter(
            transport=_PagedTransport(responses=responses.copy())
        ).read_complete_candle_pages(
            market=BrokerMarket.KRX_STOCK,
            symbol="005930",
            interval=TossCandleInterval.ONE_MINUTE,
            count=200,
            before=observed_at,
            adjusted=True,
            access_token="market-token",
            max_pages=2,
        )


@pytest.mark.asyncio
async def test_toss_candle_tail_returns_a_terminal_page() -> None:
    transport = _PagedTransport(
        responses=[
            _candle_page_response(
                timestamp="2026-08-11T23:59:00+00:00", next_before=None
            )
        ]
    )

    pages = await TossMarketDataReadOnlyAdapter(
        transport=transport
    ).read_recent_candle_pages(
        market=BrokerMarket.KRX_STOCK,
        symbol="005930",
        interval=TossCandleInterval.ONE_MINUTE,
        count=200,
        before=datetime(2026, 8, 12, 0, 0, tzinfo=UTC),
        adjusted=True,
        access_token="market-token",
        max_pages=2,
    )

    assert len(pages) == 1


@pytest.mark.asyncio
async def test_toss_candle_tail_rejects_an_empty_continuation_page() -> None:
    transport = _PagedTransport(
        responses=[
            BrokerResponse(
                status=200,
                body=b'{"result":{"candles":[],"nextBefore":"2026-08-11T23:59:00+00:00"}}',
            )
        ]
    )

    with pytest.raises(TossIncompleteCandleSnapshot, match="did not advance"):
        await TossMarketDataReadOnlyAdapter(
            transport=transport
        ).read_recent_candle_pages(
            market=BrokerMarket.KRX_STOCK,
            symbol="005930",
            interval=TossCandleInterval.ONE_MINUTE,
            count=200,
            before=datetime(2026, 8, 12, 0, 0, tzinfo=UTC),
            adjusted=True,
            access_token="market-token",
            max_pages=2,
        )


@pytest.mark.asyncio
async def test_toss_candle_tail_rejects_a_non_advancing_cursor() -> None:
    observed_at = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
    transport = _PagedTransport(
        responses=[
            _candle_page_response(
                timestamp="2026-08-12T00:00:00+00:00",
                next_before="2026-08-12T00:00:00+00:00",
            )
        ]
    )

    with pytest.raises(TossIncompleteCandleSnapshot, match="did not advance"):
        await TossMarketDataReadOnlyAdapter(
            transport=transport
        ).read_recent_candle_pages(
            market=BrokerMarket.KRX_STOCK,
            symbol="005930",
            interval=TossCandleInterval.ONE_MINUTE,
            count=200,
            before=observed_at,
            adjusted=True,
            access_token="market-token",
            max_pages=2,
        )


@pytest.mark.asyncio
async def test_toss_candle_tail_rejects_an_empty_continuation_at_the_page_cap() -> None:
    transport = _PagedTransport(
        responses=[
            BrokerResponse(
                status=200,
                body=b'{"result":{"candles":[],"nextBefore":"2026-08-11T23:59:00+00:00"}}',
            )
        ]
    )

    with pytest.raises(TossIncompleteCandleSnapshot, match="did not advance"):
        await TossMarketDataReadOnlyAdapter(
            transport=transport
        ).read_recent_candle_pages(
            market=BrokerMarket.KRX_STOCK,
            symbol="005930",
            interval=TossCandleInterval.ONE_MINUTE,
            count=200,
            before=datetime(2026, 8, 12, 0, 0, tzinfo=UTC),
            adjusted=True,
            access_token="market-token",
            max_pages=1,
        )


@pytest.mark.asyncio
async def test_toss_candle_tail_rejects_a_non_advancing_cursor_at_the_page_cap() -> (
    None
):
    observed_at = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
    transport = _PagedTransport(
        responses=[
            _candle_page_response(
                timestamp="2026-08-12T00:00:00+00:00",
                next_before="2026-08-12T00:00:00+00:00",
            )
        ]
    )

    with pytest.raises(TossIncompleteCandleSnapshot, match="did not advance"):
        await TossMarketDataReadOnlyAdapter(
            transport=transport
        ).read_recent_candle_pages(
            market=BrokerMarket.KRX_STOCK,
            symbol="005930",
            interval=TossCandleInterval.ONE_MINUTE,
            count=200,
            before=observed_at,
            adjusted=True,
            access_token="market-token",
            max_pages=1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("max_pages", (True, False))
async def test_toss_candle_tail_rejects_boolean_page_caps_before_transport(
    max_pages: bool,
) -> None:
    transport = _SinglePageTransport()

    with pytest.raises(ValueError, match="request is invalid"):
        await TossMarketDataReadOnlyAdapter(
            transport=transport
        ).read_recent_candle_pages(
            market=BrokerMarket.KRX_STOCK,
            symbol="005930",
            interval=TossCandleInterval.ONE_MINUTE,
            count=200,
            before=datetime(2026, 8, 12, 0, 0, tzinfo=UTC),
            adjusted=True,
            access_token="market-token",
            max_pages=max_pages,
        )

    assert transport.requests == []
