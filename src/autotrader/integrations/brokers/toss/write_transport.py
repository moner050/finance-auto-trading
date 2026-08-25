from __future__ import annotations

from autotrader.integrations.brokers.common import (
    BrokerRequest,
    HttpOpener,
    WhitelistedHttpsTransport,
)

_MAX_ORDER_RESPONSE_BYTES = 1024 * 1024


class TossOrderWriteHttpsTransport(WhitelistedHttpsTransport):
    """Injected-opener transport for the one exact Toss order-create route."""

    def __init__(self, *, opener: HttpOpener) -> None:
        super().__init__(
            base_url="https://openapi.tossinvest.com",
            allowed_routes=frozenset({("POST", "/api/v1/orders")}),
            opener=opener,
            max_response_bytes=_MAX_ORDER_RESPONSE_BYTES,
        )

    def _route_is_allowed(self, request: BrokerRequest) -> bool:
        return request.method == "POST" and request.path == "/api/v1/orders"
