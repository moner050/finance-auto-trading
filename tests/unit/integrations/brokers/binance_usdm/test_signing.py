from __future__ import annotations

import hashlib
import hmac

import pytest
from pydantic import SecretStr

from autotrader.integrations.brokers.binance_usdm.signing import sign_query


def test_signs_the_official_usdm_hmac_vector_in_parameter_order() -> None:
    parameters = (
        ("symbol", "BTCUSDT"),
        ("side", "BUY"),
        ("type", "LIMIT"),
        ("quantity", "1"),
        ("price", "9000"),
        ("timeInForce", "GTC"),
        ("recvWindow", "5000"),
        ("timestamp", "1591702613943"),
    )

    assert (
        sign_query(
            parameters,
            SecretStr(
                "2b5eb11e18796d12d88f13dc27dbbd02c2cc51ff7059765ed9821957d82bb4d9"
            ),
        )
        == "3c661234138461fcc7a7d8746c6558c9842d4e10870d2ecbedf7777cad694af9"
    )


def test_percent_encoding_is_stable_and_rfc3986_safe() -> None:
    secret = SecretStr("private-signing-key")
    encoded = b"clientAlgoId=a%20b%2F%2B~"
    expected = hmac.new(
        b"private-signing-key",
        encoded,
        hashlib.sha256,
    ).hexdigest()

    assert sign_query((("clientAlgoId", "a b/+~"),), secret) == expected
    assert sign_query((("clientAlgoId", "a b/+~"),), secret) == expected


def test_parameter_order_is_preserved_and_duplicate_keys_fail_closed() -> None:
    secret = SecretStr("private-signing-key")

    first = sign_query((("symbol", "BTCUSDT"), ("side", "BUY")), secret)
    second = sign_query((("side", "BUY"), ("symbol", "BTCUSDT")), secret)

    assert first != second
    with pytest.raises(ValueError, match="parameters"):
        sign_query((("symbol", "BTCUSDT"), ("symbol", "ETHUSDT")), secret)


@pytest.mark.parametrize(
    "parameters",
    (
        (),
        (("", "BTCUSDT"),),
        (("symbol", ""),),
        (("symbol\n", "BTCUSDT"),),
        (("symbol", "BTC\nUSDT"),),
    ),
)
def test_invalid_parameters_do_not_expose_secret_material(
    parameters: tuple[tuple[str, str], ...],
) -> None:
    secret = SecretStr("never-log-this-secret")

    with pytest.raises((TypeError, ValueError)) as raised:
        sign_query(parameters, secret)

    assert "never-log" not in repr(raised.value)


def test_requires_an_exact_nonblank_secretstr() -> None:
    with pytest.raises(TypeError, match="SecretStr"):
        sign_query((("symbol", "BTCUSDT"),), "plain-text")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="secret"):
        sign_query((("symbol", "BTCUSDT"),), SecretStr(" "))
