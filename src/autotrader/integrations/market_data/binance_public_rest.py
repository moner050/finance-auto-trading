"""Read-only Binance USD-M market data over the public REST endpoints.

Klines and aggregate trades need no credentials, which makes them the one
market this project can observe end to end without an account. The broker
transport signs every request, so it cannot be reused here.
"""

from __future__ import annotations

from typing import cast

import httpx

_BASE_URL = "https://fapi.binance.com"
_KLINES = "/fapi/v1/klines"
_AGG_TRADES = "/fapi/v1/aggTrades"
_EXCHANGE_INFO = "/fapi/v1/exchangeInfo"
_BOOK_TICKER = "/fapi/v1/ticker/bookTicker"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class BinancePublicRestError(RuntimeError):
    """Raised when Binance did not answer with usable market data."""


class BinancePublicRest:
    """Implements the market-data REST protocol against the public API."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        base_url: str = _BASE_URL,
    ) -> None:
        if type(base_url) is not str or not base_url.startswith("https://"):
            raise ValueError("base_url must be an HTTPS origin")
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._owned = client is None

    async def __aenter__(self) -> BinancePublicRest:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def klines(
        self, *, symbol: str, interval: str, end_time_ms: int, limit: int
    ) -> tuple[object, ...]:
        return await self._get(
            _KLINES,
            {
                "symbol": symbol,
                "interval": interval,
                "endTime": str(end_time_ms),
                "limit": str(limit),
            },
        )

    async def aggregate_trades(
        self, *, symbol: str, from_id: int, limit: int
    ) -> tuple[object, ...]:
        return await self._get(
            _AGG_TRADES,
            {"symbol": symbol, "fromId": str(from_id), "limit": str(limit)},
        )

    async def exchange_info(self, *, symbol: str) -> dict[str, object]:
        """The venue's own filters for one symbol.

        Tick size and lot size decide the price and quantity of a real order,
        so they are read from the exchange rather than configured. A number
        typed into a settings file is a number that can be wrong while every
        order it shapes still looks reasonable.
        """
        payload = await self._get_object(_EXCHANGE_INFO, {"symbol": symbol})
        found = payload.get("symbols")
        if not isinstance(found, list):
            raise BinancePublicRestError(f"{_EXCHANGE_INFO} sent no symbols")
        # The USD-M endpoint ignores the symbol parameter and answers with
        # every listed contract, so the one asked for is selected here. It is
        # still sent, because a venue that starts honouring it costs nothing.
        for item in cast("list[object]", found):
            if isinstance(item, dict) and item.get("symbol") == symbol:  # type: ignore[union-attr]
                return cast("dict[str, object]", item)
        raise BinancePublicRestError(f"{_EXCHANGE_INFO} does not list {symbol}")

    async def exchange_info_all(self) -> dict[str, object]:
        """Every listed contract, which is how the venue defines its market."""
        return await self._get_object(_EXCHANGE_INFO, {})

    async def book_ticker(self, *, symbol: str) -> dict[str, object]:
        """The best bid and ask, which is what a spread is."""
        return await self._get_object(_BOOK_TICKER, {"symbol": symbol})

    async def _get_object(self, path: str, params: dict[str, str]) -> dict[str, object]:
        payload = await self._request(path, params)
        if not isinstance(payload, dict):
            raise BinancePublicRestError(f"{path} did not answer with an object")
        return cast("dict[str, object]", payload)

    async def _get(self, path: str, params: dict[str, str]) -> tuple[object, ...]:
        payload = await self._request(path, params)
        if not isinstance(payload, list):
            raise BinancePublicRestError(f"{path} did not answer with a list")
        return tuple(cast("list[object]", payload))

    async def _request(self, path: str, params: dict[str, str]) -> object:
        client = self._ensure_client()
        try:
            response = await client.get(
                f"{self._base_url}{path}", params=params, timeout=_TIMEOUT
            )
        except httpx.HTTPError as error:
            raise BinancePublicRestError(f"{path} could not be reached") from error
        if response.status_code != 200:
            # The body can carry a rate-limit ban, so keep the status visible.
            raise BinancePublicRestError(f"{path} answered {response.status_code}")
        return response.json()

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owned = True
        return self._client


__all__ = ("BinancePublicRest", "BinancePublicRestError")
