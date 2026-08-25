from __future__ import annotations

from pathlib import Path

from autotrader.integrations.brokers.binance_usdm.secrets import (
    resolve_binance_usdm_secret,
)
from autotrader.observability.logging import redact_sensitive_values

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = "secret://dotenv/binance/usdm/live"


def test_binance_secret_is_redacted_from_structured_logging() -> None:
    secret = resolve_binance_usdm_secret(
        REFERENCE,
        {
            "BINANCE_USDM_API_KEY": "private-api-key",
            "BINANCE_USDM_SECRET_KEY": "private-secret-key",
        },
    )

    redacted = redact_sensitive_values(
        {
            "api_key": secret.api_key.get_secret_value(),
            "secret_key": secret.secret_key.get_secret_value(),
            "secret_reference": REFERENCE,
        }
    )

    assert redacted == {
        "api_key": "[REDACTED]",
        "secret_key": "[REDACTED]",
        "secret_reference": REFERENCE,
    }


def test_binance_raw_secrets_have_no_persistence_model_columns() -> None:
    model_files = tuple((ROOT / "src/autotrader/persistence/mysql/models").glob("*.py"))
    persisted_source = "\n".join(
        path.read_text(encoding="utf-8") for path in model_files
    )

    assert "BINANCE_USDM_API_KEY" not in persisted_source
    assert "BINANCE_USDM_SECRET_KEY" not in persisted_source
    assert "api_key: Mapped" not in persisted_source
    assert "secret_key: Mapped" not in persisted_source
