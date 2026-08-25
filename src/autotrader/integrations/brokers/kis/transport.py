from __future__ import annotations

from autotrader.integrations.brokers.common import (
    HttpOpener,
    WhitelistedHttpsTransport,
)

_ALLOWED_ROUTES = frozenset(
    {
        ("POST", "/oauth2/tokenP"),
        ("GET", "/uapi/domestic-stock/v1/quotations/inquire-price"),
        (
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
        ),
        (
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        ),
        (
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-index-category-price",
        ),
        (
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-index-daily-price",
        ),
        ("GET", "/uapi/domestic-stock/v1/quotations/inquire-vi-status"),
        ("GET", "/uapi/domestic-stock/v1/trading/inquire-balance"),
        ("GET", "/uapi/domestic-stock/v1/trading/inquire-psbl-order"),
        ("GET", "/uapi/overseas-futureoption/v1/quotations/inquire-price"),
        (
            "GET",
            "/uapi/overseas-futureoption/v1/quotations/inquire-time-futurechartprice",
        ),
        (
            "GET",
            "/uapi/overseas-futureoption/v1/quotations/search-contract-detail",
        ),
        ("GET", "/uapi/overseas-futureoption/v1/trading/inquire-unpd"),
        ("GET", "/uapi/overseas-futureoption/v1/trading/inquire-daily-order"),
    }
)


class KisHttpsTransport(WhitelistedHttpsTransport):
    """Real HTTPS transport limited to documented KIS read and OAuth routes."""

    def __init__(self, *, opener: HttpOpener | None = None) -> None:
        super().__init__(
            base_url="https://openapi.koreainvestment.com:9443",
            allowed_routes=_ALLOWED_ROUTES,
            opener=opener,
        )


class KisPaperHttpsTransport(WhitelistedHttpsTransport):
    """Paper HTTPS transport limited to documented KIS read and OAuth routes."""

    def __init__(self, *, opener: HttpOpener | None = None) -> None:
        super().__init__(
            base_url="https://openapivts.koreainvestment.com:29443",
            allowed_routes=_ALLOWED_ROUTES,
            opener=opener,
        )
