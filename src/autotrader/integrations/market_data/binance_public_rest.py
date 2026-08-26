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

    async def _get(self, path: str, params: dict[str, str]) -> tuple[object, ...]:
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
        payload = response.json()
        if not isinstance(payload, list):
            raise BinancePublicRestError(f"{path} did not answer with a list")
        return tuple(cast("list[object]", payload))

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owned = True
        return self._client


__all__ = ("BinancePublicRest", "BinancePublicRestError")
