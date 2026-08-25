from __future__ import annotations

from hashlib import sha256

from autotrader.strategies.david_v6.manifest import (
    STRATEGY_CODE,
    STRATEGY_VERSION,
    V6_DESIGN_SHA256,
    V6_SOURCE_SHA256,
    canonical_manifest_bytes,
    v6_configuration_hash,
)

EXPECTED_CANONICAL_MANIFEST = (
    b'{"design_sha256":"6eb312fddb74db395806015463606db0968877d459db65053'
    b'bec7bbca9e848a2","operator_overrides":{"binance_fixed_cap_usdt":null,'
    b'"binance_initial_universe":["BTCUSDT"],"binance_leverage":{"dynamic":true,'
    b'"maximum":7,"minimum":1},"binance_risk_fraction":{"A":"0.0050",'
    b'"A_CANDIDATE":"0.0025","NORMAL":"0.0025","absolute_ceiling":"0.0075"},'
    b'"cash_risk_fraction":{"A":"0.0025","NORMAL":"0.0015"},'
    b'"ceros_authority":"TELEMETRY_ONLY","fibonacci":{"25":"OBSERVATION_ONLY",'
    b'"50":"RESEARCH_ONLY","66":"FULL_EXIT"},"general_break_even_r":"0.30",'
    b'"metodo_markets":["KRX_CASH","US_CASH"],"partial_exits":'
    b'{"1.2R":"SHADOW_ONLY","1.5R":"SHADOW_ONLY"},"rollout":'
    b'{"paper_sessions":2,"shadow_sessions":2},"utc_risk_calendar":'
    b'["BINANCE_USDM"]},"source_sha256":"202f7727b1625a07652e6f5c0d826229'
    b'e4afaa9ab50ab9012bf69138d85fe684","strategy_code":"DAVID_TRULLAS_V6",'
    b'"strategy_version":"v6.0-op-20260824.1"}'
)


def test_manifest_pins_exact_source_design_and_strategy_authority() -> None:
    assert STRATEGY_CODE == "DAVID_TRULLAS_V6"
    assert STRATEGY_VERSION == "v6.0-op-20260824.1"
    assert V6_SOURCE_SHA256.hex() == (
        "202f7727b1625a07652e6f5c0d826229e4afaa9ab50ab9012bf69138d85fe684"
    )
    assert V6_DESIGN_SHA256.hex() == (
        "6eb312fddb74db395806015463606db0968877d459db65053bec7bbca9e848a2"
    )


def test_manifest_serializes_every_operator_override_canonically() -> None:
    assert canonical_manifest_bytes() == EXPECTED_CANONICAL_MANIFEST


def test_configuration_hash_is_sha256_of_the_canonical_manifest() -> None:
    assert v6_configuration_hash() == sha256(EXPECTED_CANONICAL_MANIFEST).digest()
