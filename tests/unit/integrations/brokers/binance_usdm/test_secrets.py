from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from autotrader.config.account_secrets import (
    AccountSecretResolutionError,
    DotenvAccountSecretResolver,
)
from autotrader.integrations.brokers.binance_usdm.secrets import (
    BinanceUsdmSecret,
    binance_usdm_api_key_fingerprint,
    resolve_binance_usdm_secret,
)

REFERENCE = "secret://dotenv/binance/usdm/live"


def env() -> dict[str, str]:
    return {
        "BINANCE_USDM_API_KEY": "private-binance-api-key",
        "BINANCE_USDM_SECRET_KEY": "private-binance-secret-key",
        "MYSQL_PASSWORD": "must-not-be-read",
    }


def test_resolves_only_the_exact_live_usdm_reference() -> None:
    secret = resolve_binance_usdm_secret(REFERENCE, env())

    assert type(secret) is BinanceUsdmSecret
    assert type(secret.api_key) is SecretStr
    assert secret.api_key.get_secret_value() == "private-binance-api-key"
    assert secret.secret_key.get_secret_value() == "private-binance-secret-key"


@pytest.mark.parametrize(
    "reference",
    (
        "secret://dotenv/binance/live",
        "secret://dotenv/binance/usdm/paper",
        "secret://env/binance/usdm/live",
        "BINANCE_USDM_SECRET_KEY",
    ),
)
def test_unapproved_reference_fails_without_reading_values(reference: str) -> None:
    values = env()

    with pytest.raises(
        AccountSecretResolutionError,
        match="account secret is unavailable",
    ) as raised:
        resolve_binance_usdm_secret(reference, values)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "private" not in repr(raised.value)


@pytest.mark.parametrize(
    "values",
    (
        {"BINANCE_USDM_API_KEY": "private-api-value"},
        {"BINANCE_USDM_SECRET_KEY": "private-secret-value"},
        {
            "BINANCE_USDM_API_KEY": "",
            "BINANCE_USDM_SECRET_KEY": "private-secret-value",
        },
        {"BINANCE_USDM_API_KEY": "private-api-value", "BINANCE_USDM_SECRET_KEY": " "},
        {
            "BINANCE_USDM_API_KEY": "private-bad\napi",
            "BINANCE_USDM_SECRET_KEY": "private-secret-value",
        },
    ),
)
def test_missing_blank_or_multiline_values_fail_closed(
    values: dict[str, str],
) -> None:
    with pytest.raises(AccountSecretResolutionError) as raised:
        resolve_binance_usdm_secret(REFERENCE, values)

    assert str(raised.value) == "account secret is unavailable"
    assert "private-" not in repr(raised.value)


def test_secret_object_repr_and_exception_never_disclose_values() -> None:
    secret = resolve_binance_usdm_secret(REFERENCE, env())

    rendered = repr(secret)

    assert "private-binance-api-key" not in rendered
    assert "private-binance-secret-key" not in rendered
    assert "**********" in rendered


def test_api_key_fingerprint_is_stable_redacted_and_key_specific() -> None:
    secret = resolve_binance_usdm_secret(REFERENCE, env())
    changed = resolve_binance_usdm_secret(
        REFERENCE,
        {
            **env(),
            "BINANCE_USDM_API_KEY": "different-private-api-key",
        },
    )

    fingerprint = binance_usdm_api_key_fingerprint(secret)

    assert fingerprint == binance_usdm_api_key_fingerprint(secret)
    assert fingerprint != binance_usdm_api_key_fingerprint(changed)
    assert len(fingerprint) == 32
    assert b"private" not in fingerprint


def test_explicit_dotenv_resolver_branch_reads_only_selected_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "selected.env"
    path.write_text(
        "BINANCE_USDM_API_KEY=api-key\n"
        "BINANCE_USDM_SECRET_KEY=secret-key\n"
        "MYSQL_PASSWORD=unrelated-private\n",
        encoding="utf-8",
    )

    secret = DotenvAccountSecretResolver(path).resolve_binance_usdm(REFERENCE)

    assert secret.api_key.get_secret_value() == "api-key"
    assert secret.secret_key.get_secret_value() == "secret-key"
    assert "unrelated-private" not in repr(secret)
