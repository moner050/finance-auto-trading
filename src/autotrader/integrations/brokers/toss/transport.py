from __future__ import annotations

from datetime import date

from autotrader.integrations.brokers.common import (
    BrokerRequest,
    HttpOpener,
    WhitelistedHttpsTransport,
)


class TossHttpsTransport(WhitelistedHttpsTransport):
    """Real HTTPS transport limited to documented Toss read and OAuth routes."""

    def __init__(self, *, opener: HttpOpener | None = None) -> None:
        super().__init__(
            base_url="https://openapi.tossinvest.com",
            allowed_routes=frozenset(
                {
                    ("POST", "/oauth2/token"),
                    ("GET", "/api/v1/orderbook"),
                    ("GET", "/api/v1/market-calendar/KR"),
                    ("GET", "/api/v1/accounts"),
                    ("GET", "/api/v1/buying-power"),
                    ("GET", "/api/v1/holdings"),
                    ("GET", "/api/v1/sellable-quantity"),
                    ("GET", "/api/v1/orders"),
                    ("GET", "/api/v1/prices"),
                    ("GET", "/api/v1/trades"),
                    ("GET", "/api/v1/candles"),
                }
            ),
            opener=opener,
        )

    def _route_is_allowed(self, request: BrokerRequest) -> bool:
        if request.path.startswith("/api/v1/market-calendar/KR"):
            return _is_kr_market_calendar_read(request)
        return (
            _is_order_detail_read(request)
            or _is_stock_warning_read(request)
            or super()._route_is_allowed(request)
        )


def _is_order_detail_read(request: BrokerRequest) -> bool:
    prefix = "/api/v1/orders/"
    if request.method != "GET" or not request.path.startswith(prefix):
        return False
    order_id = request.path.removeprefix(prefix)
    return bool(order_id) and "/" not in order_id and "?" not in order_id


def _is_stock_warning_read(request: BrokerRequest) -> bool:
    prefix = "/api/v1/stocks/"
    suffix = "/warnings"
    if (
        request.method != "GET"
        or "?" in request.path
        or "#" in request.path
        or not request.path.startswith(prefix)
        or not request.path.endswith(suffix)
    ):
        return False
    symbol = request.path[len(prefix) : -len(suffix)]
    return (
        bool(symbol)
        and symbol not in {".", ".."}
        and all(character not in symbol for character in "/%\\")
    )


def _is_kr_market_calendar_read(request: BrokerRequest) -> bool:
    path = "/api/v1/market-calendar/KR"
    if request.method != "GET":
        return False
    if request.path == path:
        return True
    prefix = f"{path}?date="
    if not request.path.startswith(prefix):
        return False
    value = request.path.removeprefix(prefix)
    try:
        return len(value) == 10 and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False
