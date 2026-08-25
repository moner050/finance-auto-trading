from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

STRATEGY_CODE = "DAVID_TRULLAS_V6"
STRATEGY_VERSION = "v6.0-op-20260824.1"
V6_SOURCE_SHA256 = bytes.fromhex(
    "202f7727b1625a07652e6f5c0d826229e4afaa9ab50ab9012bf69138d85fe684"
)
V6_DESIGN_SHA256 = bytes.fromhex(
    "6eb312fddb74db395806015463606db0968877d459db65053bec7bbca9e848a2"
)

_CONFIGURATION_MANIFEST = {
    "design_sha256": V6_DESIGN_SHA256.hex(),
    "operator_overrides": {
        "binance_fixed_cap_usdt": None,
        "binance_initial_universe": ["BTCUSDT"],
        "binance_leverage": {
            "dynamic": True,
            "maximum": 7,
            "minimum": 1,
        },
        "binance_risk_fraction": {
            "A": "0.0050",
            "A_CANDIDATE": "0.0025",
            "NORMAL": "0.0025",
            "absolute_ceiling": "0.0075",
        },
        "cash_risk_fraction": {
            "A": "0.0025",
            "NORMAL": "0.0015",
        },
        "ceros_authority": "TELEMETRY_ONLY",
        "fibonacci": {
            "25": "OBSERVATION_ONLY",
            "50": "RESEARCH_ONLY",
            "66": "FULL_EXIT",
        },
        "general_break_even_r": "0.30",
        "metodo_markets": ["KRX_CASH", "US_CASH"],
        "partial_exits": {
            "1.2R": "SHADOW_ONLY",
            "1.5R": "SHADOW_ONLY",
        },
        "rollout": {
            "paper_sessions": 2,
            "shadow_sessions": 2,
        },
        "utc_risk_calendar": ["BINANCE_USDM"],
    },
    "source_sha256": V6_SOURCE_SHA256.hex(),
    "strategy_code": STRATEGY_CODE,
    "strategy_version": STRATEGY_VERSION,
}


@dataclass(frozen=True, slots=True)
class V6Manifest:
    id: UUID
    strategy_version_id: UUID
    source_sha256: bytes
    design_sha256: bytes
    configuration_hash: bytes
    registered_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "strategy_version_id"):
            value = getattr(self, name)
            if not isinstance(value, UUID) or value.version != 7:
                raise ValueError(f"{name} must be UUIDv7")
        expected_hashes = {
            "source_sha256": V6_SOURCE_SHA256,
            "design_sha256": V6_DESIGN_SHA256,
            "configuration_hash": v6_configuration_hash(),
        }
        for name, expected in expected_hashes.items():
            if getattr(self, name) != expected:
                raise ValueError(f"{name} must match the canonical v6 manifest")
        registered_at = cast(object, self.registered_at)
        if (
            not isinstance(registered_at, datetime)
            or registered_at.tzinfo is None
            or registered_at.utcoffset() != UTC.utcoffset(registered_at)
            or registered_at.microsecond != 0
        ):
            raise ValueError("registered_at must be exact whole-second UTC")


def canonical_manifest_bytes() -> bytes:
    return json.dumps(
        _CONFIGURATION_MANIFEST,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def v6_configuration_hash() -> bytes:
    return hashlib.sha256(canonical_manifest_bytes()).digest()
