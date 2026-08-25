from dataclasses import FrozenInstanceError

import pytest

from autotrader.domain.enums import BrokerProvider
from autotrader.integrations.brokers.common import (
    BrokerCapability,
    BrokerMarket,
    BrokerRequest,
    BrokerResponse,
)


def test_binance_usdm_primitives_are_explicit() -> None:
    assert BrokerProvider.BINANCE.value == "BINANCE"
    assert BrokerMarket.BINANCE_USDM.value == "BINANCE_USDM"
    assert BrokerCapability.USD_M_FUTURES.value == "USD_M_FUTURES"


def test_broker_request_requires_a_relative_path() -> None:
    with pytest.raises(ValueError, match="relative"):
        BrokerRequest(method="GET", path="https://broker.example/orders")


def test_broker_request_is_immutable_and_normalizes_header_order() -> None:
    request = BrokerRequest(
        method="get",
        path="/market/prices",
        headers=(("z-header", "two"), ("a-header", "one")),
    )

    assert request.method == "GET"
    assert request.headers == (("a-header", "one"), ("z-header", "two"))
    with pytest.raises(FrozenInstanceError):
        request.path = "/changed"  # type: ignore[misc]


def test_broker_request_normalizes_delete_for_exact_route_allowlisting() -> None:
    request = BrokerRequest(method="delete", path="/fapi/v1/order")

    assert request.method == "DELETE"


def test_broker_request_keeps_put_unsupported() -> None:
    with pytest.raises(ValueError, match="GET, POST, or DELETE"):
        BrokerRequest(method="PUT", path="/fapi/v1/order")


def test_broker_response_requires_a_http_status() -> None:
    with pytest.raises(ValueError, match="HTTP"):
        BrokerResponse(status=99, body=b"{}")


def test_broker_response_preserves_case_insensitive_provider_headers() -> None:
    response = BrokerResponse(
        status=200,
        body=b"{}",
        headers=(("x-z", "two"), ("Tr_Cont", "M")),
    )

    assert response.headers == (("Tr_Cont", "M"), ("x-z", "two"))
    assert response.header("tr_cont") == "M"


def test_broker_response_preserves_empty_terminal_header_value() -> None:
    response = BrokerResponse(
        status=200,
        body=b"{}",
        headers=(("tr_cont", ""),),
    )

    assert response.header("tr_cont") == ""


@pytest.mark.parametrize(
    ("header_name", "header_value"),
    (("", "value"), ("tr_cont\n", "M"), ("tr_cont", "M\n")),
)
def test_broker_response_rejects_empty_or_multiline_header_names_and_values(
    header_name: str,
    header_value: str,
) -> None:
    with pytest.raises(ValueError, match="non-empty single lines"):
        BrokerResponse(
            status=200,
            body=b"{}",
            headers=((header_name, header_value),),
        )
