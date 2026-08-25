from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[5]
EVIDENCE = (
    ROOT / "docs/providers/toss/openapi-1.2.14-rate-limit-contract.sanitized.json"
)


def _evidence() -> dict[str, Any]:
    return json.loads(EVIDENCE.read_text(encoding="utf-8"))


def _validate(value: dict[str, Any]) -> None:
    assert value["schemaVersion"] == 1
    assert value["sources"] == {
        "openapi": {
            "url": "https://openapi.tossinvest.com/openapi-docs/latest/openapi.json",
            "hashOf": "RAW_RESPONSE_BYTES",
            "openapi": "3.1.0",
            "version": "1.2.14",
            "sha256": (
                "fccf49abd11f37f557bdd349138f4a03c42b829ebd8b5c14ab4907116fb84c7a"
            ),
        },
        "overview": {
            "url": "https://openapi.tossinvest.com/openapi-docs/overview.md",
            "hashOf": "RAW_RESPONSE_BYTES",
            "sha256": (
                "dfad8c9251917daf39d2b2a9e455f0d7cadddafb42a34f47b2ee8d67bf4addd8"
            ),
        },
    }
    assert value["reviewedSourceChange"] == {
        "previousOpenapiSha256": (
            "d29f9079a557c0b6affcec330aa131f93b09fd49932354668e3dc4524cd42180"
        ),
        "previousOverviewSha256": (
            "747954c11b38e683792efc0f604a163bc4b126e4a1ea80935ec280ad13a43c2a"
        ),
        "rateLimitSemanticsChanged": False,
    }
    assert value["endpointGroups"] == {
        "oauth": "AUTH",
        "accounts": "ACCOUNT",
        "holdings": "ASSET",
        "buyingPower": "ORDER_INFO",
        "sellableQuantity": "ORDER_INFO",
        "openOrders": "ORDER_HISTORY",
    }
    assert value["limits"] == {
        "AUTH": {"normalTps": 5},
        "ACCOUNT": {"normalTps": 1},
        "ASSET": {"normalTps": 5},
        "ORDER_INFO": {
            "normalTps": 6,
            "peakKst": "09:00:00/09:10:00",
            "peakTps": 3,
        },
        "ORDER_HISTORY": {"normalTps": 5},
    }
    assert value["headers"] == {
        "limit": "X-RateLimit-Limit",
        "remaining": "X-RateLimit-Remaining",
        "resetSeconds": "X-RateLimit-Reset",
        "retryAfterSeconds": "Retry-After",
    }
    assert value["clientPolicy"] == {
        "documentedLimitsAreInitialCaps": True,
        "dynamicLimitMayOnlyLowerCap": True,
        "maximum429Retries": 1,
        "retryMustFitAbsoluteDeadline": True,
        "credentialOrPayloadExamplesStored": False,
    }


def test_toss_rate_limit_evidence_pins_exact_contract() -> None:
    _validate(_evidence())


@pytest.mark.parametrize(
    ("section", "key"),
    (
        ("endpointGroups", "buyingPower"),
        ("limits", "ORDER_INFO"),
        ("headers", "remaining"),
        ("clientPolicy", "maximum429Retries"),
    ),
)
def test_toss_rate_limit_evidence_rejects_removed_contract_fields(
    section: str, key: str
) -> None:
    mutated = copy.deepcopy(_evidence())
    del mutated[section][key]

    with pytest.raises(AssertionError):
        _validate(mutated)


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("endpointGroups", "sellableQuantity"), "ORDER"),
        (("limits", "ORDER_INFO", "normalTps"), 7),
        (("limits", "ORDER_INFO", "peakTps"), 4),
        (("limits", "ORDER_INFO", "peakKst"), "09:00:00/09:11:00"),
        (("headers", "limit"), "X-Other-Limit"),
        (("clientPolicy", "maximum429Retries"), 2),
    ),
)
def test_toss_rate_limit_evidence_rejects_widened_contract(
    path: tuple[str, ...], value: object
) -> None:
    mutated = copy.deepcopy(_evidence())
    target: dict[str, Any] = mutated
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(AssertionError):
        _validate(mutated)
