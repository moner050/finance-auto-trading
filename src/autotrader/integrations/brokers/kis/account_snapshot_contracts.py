from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self, cast

_SCOPE = "KR_DOMESTIC_SIX_DIGIT_ACCOUNT_PRODUCTS"
_CASH_MEANING = "TOTAL_DEPOSIT_CASH"


class KisDomesticCashEnvironment(StrEnum):
    REAL = "REAL"
    PAPER = "PAPER"


@dataclass(frozen=True, slots=True)
class KisKrDomesticCashPosition:
    symbol: str
    total_quantity: Decimal
    order_available_quantity: Decimal

    def __post_init__(self) -> None:
        total = _amount(self.total_quantity)
        available = _amount(self.order_available_quantity)
        if not _digits(self.symbol, length=6) or total <= 0 or available > total:
            raise ValueError("KIS domestic cash position is invalid")
        object.__setattr__(self, "total_quantity", total)
        object.__setattr__(self, "order_available_quantity", available)


@dataclass(frozen=True, slots=True)
class KisStableKrDomesticCashAccountSnapshot:
    observed_at: datetime
    environment: KisDomesticCashEnvironment
    total_deposit_cash: Decimal
    positions: tuple[KisKrDomesticCashPosition, ...]
    source_hash: bytes
    scope: str = field(default=_SCOPE, init=False)
    cash_meaning: str = field(default=_CASH_MEANING, init=False)

    @classmethod
    def build(
        cls,
        *,
        observed_at: datetime,
        environment: KisDomesticCashEnvironment,
        total_deposit_cash: Decimal,
        positions: tuple[KisKrDomesticCashPosition, ...],
    ) -> Self:
        cash = _amount(total_deposit_cash)
        exact_positions = _validated_positions(positions)
        return cls(
            observed_at=observed_at,
            environment=environment,
            total_deposit_cash=cash,
            positions=exact_positions,
            source_hash=_snapshot_hash(environment, cash, exact_positions),
        )

    def __post_init__(self) -> None:
        cash = _amount(self.total_deposit_cash)
        positions = _validated_positions(self.positions)
        source_hash: object = self.source_hash
        if (
            type(self.observed_at) is not datetime
            or self.observed_at.tzinfo is not UTC
            or self.observed_at.microsecond != 0
            or type(self.environment) is not KisDomesticCashEnvironment
            or tuple(sorted(positions, key=lambda item: item.symbol)) != positions
            or len({position.symbol for position in positions}) != len(positions)
            or type(source_hash) is not bytes
            or len(source_hash) != 32
            or source_hash != _snapshot_hash(self.environment, cash, positions)
            or self.scope != _SCOPE
            or self.cash_meaning != _CASH_MEANING
        ):
            raise ValueError("KIS stable account snapshot is invalid")
        object.__setattr__(self, "total_deposit_cash", cash)


def _validated_positions(value: object) -> tuple[KisKrDomesticCashPosition, ...]:
    if not isinstance(value, tuple):
        raise ValueError("KIS stable account snapshot is invalid")
    values = cast(tuple[object, ...], value)
    if not all(type(position) is KisKrDomesticCashPosition for position in values):
        raise ValueError("KIS stable account snapshot is invalid")
    positions = cast(tuple[KisKrDomesticCashPosition, ...], values)
    for position in positions:
        position.__post_init__()
    return positions


def _amount(value: object) -> Decimal:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value < 0
        or value != value.to_integral_value()
        or (value != 0 and value.adjusted() + 1 > 30)
    ):
        raise ValueError
    return Decimal(int(value))


def _digits(value: object, *, length: int | None = None) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and (length is None or len(value) == length)
        and value.isascii()
        and value.isdecimal()
    )


def _snapshot_hash(
    environment: KisDomesticCashEnvironment,
    total_deposit_cash: Decimal,
    positions: tuple[KisKrDomesticCashPosition, ...],
) -> bytes:
    payload = {
        "cashMeaning": _CASH_MEANING,
        "environment": environment.value,
        "positions": [
            {
                "orderAvailableQuantity": format(
                    position.order_available_quantity, "f"
                ),
                "symbol": position.symbol,
                "totalQuantity": format(position.total_quantity, "f"),
            }
            for position in positions
        ],
        "provider": "KIS",
        "scope": _SCOPE,
        "totalDepositCash": format(total_deposit_cash, "f"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).digest()
