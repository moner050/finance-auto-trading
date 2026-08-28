"""The put-call ratio, read from the venue that has the options.

The strategy wants a put-call percentile. Binance USD-M lists no options, so
the ratio is read where BTC options actually trade. This is the only place in
the project that talks to a second venue, and it does so read-only, over the
public API, for one number.

Volume, not open interest. The equity put-call ratio the strategy was written
against is a volume ratio — what was traded today, not what is outstanding —
and the two say different things: open interest moves slowly and volume is the
day's opinion. Deribit publishes the daily call and put volumes as an
aggregate, so the ratio is its arithmetic rather than ours.

Deribit publishes no daily history of this, only rolling 24-hour, 7-day and
30-day totals. A percentile therefore cannot be computed from a single read:
the ratio has to be recorded each day and ranked against what accumulated.
This module reads one day. It does not pretend to a history it cannot see.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast

import httpx

from autotrader.shared.time import require_utc

BASE_URL = "https://www.deribit.com"
_TRADE_VOLUMES = "/api/v2/public/get_trade_volumes"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

CURRENCY = "BTC"


class DeribitError(RuntimeError):
    """Raised when Deribit did not answer with a usable put-call reading."""


@dataclass(frozen=True, slots=True)
class PutCallReading:
    """One day's option volumes, and the ratio between them."""

    as_of: datetime
    currency: str
    calls_volume: Decimal
    puts_volume: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", require_utc(self.as_of))
        for name in ("calls_volume", "puts_volume"):
            value = getattr(self, name)
            if type(value) is not Decimal or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a non-negative finite Decimal")

    @property
    def ratio(self) -> Decimal:
        """Puts over calls.

        A day with no call volume has no ratio rather than an enormous one.
        Reporting a division by zero as a very pessimistic market would be the
        measurement failing loudly disguised as a signal.
        """
        if self.calls_volume == 0:
            raise ValueError("a day with no call volume has no put-call ratio")
        return self.puts_volume / self.calls_volume


class DeribitPublic:
    """Read-only access to Deribit's public market data."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        base_url: str = BASE_URL,
    ) -> None:
        if type(base_url) is not str or not base_url.startswith("https://"):
            raise ValueError("base_url must be an HTTPS origin")
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._owned = client is None

    async def __aenter__(self) -> DeribitPublic:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def trade_volumes(self) -> tuple[dict[str, object], ...]:
        client = self._client or httpx.AsyncClient()
        if self._client is None:
            self._client = client
            self._owned = True
        try:
            response = await client.get(
                f"{self._base_url}{_TRADE_VOLUMES}",
                params={"extended": "true"},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as error:
            raise DeribitError(f"{_TRADE_VOLUMES} could not be reached") from error
        if response.status_code != 200:
            raise DeribitError(f"{_TRADE_VOLUMES} answered {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise DeribitError(f"{_TRADE_VOLUMES} did not answer with an object")
        result = cast("dict[str, object]", payload).get("result")
        if not isinstance(result, list):
            raise DeribitError(f"{_TRADE_VOLUMES} sent no result")
        return tuple(
            cast("dict[str, object]", item)
            for item in cast("list[object]", result)
            if isinstance(item, dict)
        )


async def read_put_call(
    deribit: DeribitPublic, *, now: datetime, currency: str = CURRENCY
) -> PutCallReading:
    """One day's call and put volumes for a currency."""
    for entry in await deribit.trade_volumes():
        if entry.get("currency") != currency:
            continue
        return PutCallReading(
            as_of=now,
            currency=currency,
            calls_volume=_amount(entry.get("calls_volume"), "calls_volume"),
            puts_volume=_amount(entry.get("puts_volume"), "puts_volume"),
        )
    raise DeribitError(f"Deribit reported no option volumes for {currency}")


def _amount(value: object, name: str) -> Decimal:
    # Deribit sends these as JSON numbers. Decimal is built from the repr so
    # the value that arrived is the value stored, without a float round trip
    # widening it.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DeribitError(f"{name} was not sent as a number")
    return Decimal(repr(value))


__all__ = (
    "BASE_URL",
    "CURRENCY",
    "DeribitError",
    "DeribitPublic",
    "PutCallReading",
    "read_put_call",
)
