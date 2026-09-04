from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol, cast

from autotrader.integrations.brokers.binance_usdm.account import (
    BinanceUsdmAccountSnapshot,
)
from autotrader.integrations.brokers.common import BrokerRequest, BrokerResponse
from autotrader.risk.v6 import MAX_LEVERAGE

_SYMBOL = "BTCUSDT"
_KEY_EVIDENCE_MAXIMUM_AGE = timedelta(days=1)


class BinanceUsdmConfigurationError(RuntimeError):
    """Raised when provider configuration facts are malformed or unavailable."""


class BinanceUsdmConfigurationReader(Protocol):
    async def send(self, request: BrokerRequest) -> BrokerResponse: ...


@dataclass(frozen=True, slots=True)
class BinanceUsdmApiKeyEvidence:
    captured_at: datetime
    api_key_fingerprint: bytes
    ip_restricted: bool | None
    withdrawals_enabled: bool | None

    def __post_init__(self) -> None:
        _require_utc(self.captured_at, "API key evidence captured_at")
        if (
            type(self.api_key_fingerprint) is not bytes
            or len(self.api_key_fingerprint) != 32
        ):
            raise ValueError("Binance USD-M API key fingerprint must be SHA-256")
        for value in (self.ip_restricted, self.withdrawals_enabled):
            if value is not None and type(value) is not bool:
                raise TypeError("Binance USD-M API key evidence must be bool or None")


@dataclass(frozen=True, slots=True)
class ConfigurationReport:
    ready: bool
    blockers: tuple[str, ...]
    position_mode: str
    margin_type: str
    auto_add_margin: bool
    leverage: int
    can_trade: bool
    multi_assets_margin: bool
    account_transfer_out_enabled: bool


async def verify_binance_usdm_configuration(
    *,
    reader: BinanceUsdmConfigurationReader,
    snapshot: BinanceUsdmAccountSnapshot,
    api_key_evidence: BinanceUsdmApiKeyEvidence,
    expected_leverage: int,
    as_of: datetime,
    owned_btc_position_amount: Decimal = Decimal(),
) -> ConfigurationReport:
    as_of = _require_utc(as_of, "configuration as_of")
    if type(snapshot) is not BinanceUsdmAccountSnapshot:
        raise TypeError("Binance USD-M account snapshot must be exact")
    if type(api_key_evidence) is not BinanceUsdmApiKeyEvidence:
        raise TypeError("Binance USD-M API key evidence must be exact")
    if type(expected_leverage) is not int or not 1 <= expected_leverage <= MAX_LEVERAGE:
        raise ValueError(
            f"Binance USD-M expected leverage must be 1 through {MAX_LEVERAGE}"
        )
    if (
        type(owned_btc_position_amount) is not Decimal
        or not owned_btc_position_amount.is_finite()
    ):
        raise ValueError("Binance USD-M owned position amount is invalid")

    try:
        account = _object(
            await reader.send(
                BrokerRequest(method="GET", path="/fapi/v1/accountConfig")
            )
        )
        mode = _object(
            await reader.send(
                BrokerRequest(method="GET", path="/fapi/v1/positionSide/dual")
            )
        )
        symbols = _array(
            await reader.send(
                BrokerRequest(
                    method="GET",
                    path=f"/fapi/v1/symbolConfig?symbol={_SYMBOL}",
                )
            )
        )
        if len(symbols) != 1 or symbols[0].get("symbol") != _SYMBOL:
            raise ValueError
        symbol = symbols[0]
        account_dual = _boolean(account.get("dualSidePosition"))
        mode_dual = _boolean(mode.get("dualSidePosition"))
        can_trade = _boolean(account.get("canTrade"))
        account_transfer_out = _boolean(account.get("canWithdraw"))
        multi_assets = _boolean(account.get("multiAssetsMargin"))
        margin_type = _text(symbol.get("marginType"))
        auto_add = _boolean(symbol.get("isAutoAddMargin"))
        leverage = _integer(symbol.get("leverage"))
        _decimal_text(symbol.get("maxNotionalValue"))
    except KeyboardInterrupt, SystemExit:
        raise
    except Exception:
        raise BinanceUsdmConfigurationError(
            "Binance USD-M configuration facts are unavailable"
        ) from None

    blockers: list[str] = []
    if snapshot.as_of != as_of:
        blockers.append("ACCOUNT_SNAPSHOT_TIME_MISMATCH")
    evidence_age = as_of - api_key_evidence.captured_at
    if evidence_age < timedelta(0) or evidence_age > _KEY_EVIDENCE_MAXIMUM_AGE:
        blockers.append("API_KEY_EVIDENCE_STALE")
    if api_key_evidence.ip_restricted is None:
        blockers.append("API_KEY_IP_RESTRICTION_UNPROVEN")
    elif not api_key_evidence.ip_restricted:
        blockers.append("API_KEY_IP_NOT_RESTRICTED")
    if api_key_evidence.withdrawals_enabled is None:
        blockers.append("API_KEY_WITHDRAWALS_UNPROVEN")
    elif api_key_evidence.withdrawals_enabled:
        blockers.append("API_KEY_WITHDRAWALS_ENABLED")
    if not can_trade:
        blockers.append("ACCOUNT_TRADING_DISABLED")
    if account_dual != mode_dual:
        blockers.append("POSITION_MODE_FACT_CONFLICT")
    if account_dual or mode_dual:
        blockers.append("POSITION_MODE_NOT_ONE_WAY")
    if multi_assets:
        blockers.append("MULTI_ASSET_MODE_ENABLED")
    if margin_type != "ISOLATED":
        blockers.append("MARGIN_TYPE_NOT_ISOLATED")
    if auto_add:
        blockers.append("AUTO_ADD_MARGIN_ENABLED")
    # The same ceiling as the risk engine's, read from it rather than written
    # again: two literals for one limit are two things to remember to change.
    if not 1 <= leverage <= MAX_LEVERAGE:
        blockers.append("LEVERAGE_OUT_OF_RANGE")
    elif leverage != expected_leverage:
        blockers.append("LEVERAGE_MISMATCH")
    _exposure_blockers(
        snapshot,
        owned_btc_position_amount=owned_btc_position_amount,
        blockers=blockers,
    )
    return ConfigurationReport(
        ready=not blockers,
        blockers=tuple(blockers),
        position_mode="HEDGE" if account_dual or mode_dual else "ONE_WAY",
        margin_type=margin_type,
        auto_add_margin=auto_add,
        leverage=leverage,
        can_trade=can_trade,
        multi_assets_margin=multi_assets,
        account_transfer_out_enabled=account_transfer_out,
    )


def _exposure_blockers(
    snapshot: BinanceUsdmAccountSnapshot,
    *,
    owned_btc_position_amount: Decimal,
    blockers: list[str],
) -> None:
    btc_amount = sum(
        (
            position.amount
            for position in snapshot.positions
            if position.symbol == _SYMBOL
        ),
        start=Decimal(),
    )
    if btc_amount != owned_btc_position_amount:
        blockers.append("UNOWNED_BTCUSDT_EXPOSURE")
    if any(
        position.symbol != _SYMBOL and position.amount != 0
        for position in snapshot.positions
    ):
        blockers.append("UNEXPECTED_SYMBOL_EXPOSURE")
    if any(order.symbol != _SYMBOL for order in snapshot.normal_orders):
        blockers.append("UNEXPECTED_SYMBOL_NORMAL_ORDER")
    if any(order.symbol != _SYMBOL for order in snapshot.algo_orders):
        blockers.append("UNEXPECTED_SYMBOL_ALGO_ORDER")


def _object(response: BrokerResponse) -> dict[str, object]:
    payload = _payload(response)
    if not isinstance(payload, dict):
        raise ValueError
    raw = cast(dict[object, object], payload)
    if any(type(key) is not str for key in raw):
        raise ValueError
    return cast(dict[str, object], raw)


def _array(response: BrokerResponse) -> tuple[dict[str, object], ...]:
    payload = _payload(response)
    if not isinstance(payload, list):
        raise ValueError
    result: list[dict[str, object]] = []
    for item in cast(list[object], payload):
        if not isinstance(item, dict):
            raise ValueError
        raw = cast(dict[object, object], item)
        if any(type(key) is not str for key in raw):
            raise ValueError
        result.append(cast(dict[str, object], raw))
    return tuple(result)


def _payload(response: BrokerResponse) -> object:
    if type(response) is not BrokerResponse or response.status != 200:
        raise ValueError
    try:
        return cast(object, json.loads(response.body))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError from error


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError
    return value


def _text(value: object) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError
    return value


def _decimal_text(value: object) -> Decimal:
    if type(value) is not str:
        raise ValueError
    parsed = Decimal(value)
    if not parsed.is_finite() or parsed < 0:
        raise ValueError
    return parsed


def _require_utc(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is not UTC
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"Binance USD-M {name} must use exact UTC")
    return value


__all__ = (
    "BinanceUsdmApiKeyEvidence",
    "BinanceUsdmConfigurationError",
    "BinanceUsdmConfigurationReader",
    "ConfigurationReport",
    "verify_binance_usdm_configuration",
)
