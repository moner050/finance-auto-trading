from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast
from uuid import UUID

from autotrader.domain.enums import OrderStyle, Side
from autotrader.shared.decimal import require_decimal
from autotrader.shared.time import require_utc

_MAXIMUM_RISK_FRACTION = Decimal("0.0075")


class EvidenceState(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"
    NOT_APPLICABLE = "N/A"
    UNKNOWN = "UNKNOWN"


class StrategyFamily(StrEnum):
    METODO = "METODO"
    HLIT = "HLIT"


class SetupGrade(StrEnum):
    REJECT = "REJECT"
    NORMAL = "NORMAL"
    A_CANDIDATE = "A_CANDIDATE"
    A = "A"


class V6Market(StrEnum):
    KRX_CASH = "KRX_CASH"
    US_CASH = "US_CASH"
    BINANCE_USDM = "BINANCE_USDM"


# Markets whose accounts hold the instrument itself, so a position can only be
# opened by buying it.
#
# This is a fact about the venue, and saying so matters: section 12 lists
# `permanent_long_only` among the prohibitions, and section 2.2 gives the short
# rule as the exact mirror of the long one - `downtrend_regime and
# cross_down(sma6, sma70)` - with section 2.4 naming Baxter a short candidate.
# The strategy shorts. These two accounts are spot, and spot cannot.
#
# So the refusal is the broker's, not the method's, and a margin-capable cash
# venue would belong on the other side of this line rather than inheriting a
# stance the author never took.
SPOT_ONLY_MARKETS = frozenset({V6Market.KRX_CASH, V6Market.US_CASH})


@dataclass(frozen=True, slots=True)
class MatchedIndicator:
    key: str
    mandatory: bool
    evidence_state: EvidenceState
    evidence_hash: bytes

    def __post_init__(self) -> None:
        if not self.key or self.key.strip() != self.key:
            raise ValueError("indicator key must be non-empty and trimmed")
        if type(self.mandatory) is not bool:
            raise TypeError("mandatory must be bool")
        if self.evidence_state is not EvidenceState.AVAILABLE:
            raise ValueError("matched indicator evidence must be AVAILABLE")
        _require_sha256(self.evidence_hash, "indicator evidence_hash")


@dataclass(frozen=True, slots=True)
class V6Decision:
    id: UUID
    strategy_version_id: UUID
    setup_id: UUID
    feature_snapshot_id: UUID
    instrument_id: UUID
    market: V6Market
    family: StrategyFamily
    grade: SetupGrade
    side: Side
    order_style: OrderStyle
    matched_indicators: tuple[MatchedIndicator, ...]
    blockers: tuple[str, ...]
    planned_entry: Decimal | None
    structural_stop: Decimal | None
    target_price: Decimal | None
    risk_fraction: Decimal
    calculated_quantity: Decimal
    expected_cost: Decimal | None
    source_evidence_hashes: tuple[bytes, ...]
    completed_evidence_at: datetime
    generated_at: datetime
    valid_until: datetime

    def __post_init__(self) -> None:
        for name in (
            "id",
            "strategy_version_id",
            "setup_id",
            "feature_snapshot_id",
            "instrument_id",
        ):
            _require_uuid7(getattr(self, name), name)
        for name, expected_type in (
            ("market", V6Market),
            ("family", StrategyFamily),
            ("grade", SetupGrade),
            ("side", Side),
            ("order_style", OrderStyle),
        ):
            if type(getattr(self, name)) is not expected_type:
                raise TypeError(f"{name} must be an exact {expected_type.__name__}")
        if self.family is StrategyFamily.METODO and self.market not in {
            V6Market.KRX_CASH,
            V6Market.US_CASH,
        }:
            raise ValueError("METODO is available only for cash markets")
        _require_canonical_indicators(self.matched_indicators)
        _require_sorted_unique_text(self.blockers, "blockers")
        _require_canonical_hashes(self.source_evidence_hashes)

        object.__setattr__(
            self,
            "planned_entry",
            _optional_decimal(self.planned_entry),
        )
        object.__setattr__(
            self,
            "structural_stop",
            _optional_decimal(self.structural_stop),
        )
        object.__setattr__(
            self,
            "target_price",
            _optional_decimal(self.target_price),
        )
        object.__setattr__(
            self,
            "risk_fraction",
            require_decimal(self.risk_fraction),
        )
        object.__setattr__(
            self,
            "calculated_quantity",
            require_decimal(self.calculated_quantity),
        )
        object.__setattr__(
            self,
            "expected_cost",
            _optional_decimal(self.expected_cost),
        )
        object.__setattr__(
            self,
            "completed_evidence_at",
            require_utc(self.completed_evidence_at),
        )
        object.__setattr__(self, "generated_at", require_utc(self.generated_at))
        object.__setattr__(self, "valid_until", require_utc(self.valid_until))

        if not Decimal("0") <= self.risk_fraction <= _MAXIMUM_RISK_FRACTION:
            raise ValueError("risk fraction exceeds the absolute ceiling")
        if self.calculated_quantity < 0:
            raise ValueError("calculated quantity must be non-negative")
        if self.completed_evidence_at > self.generated_at:
            raise ValueError("completed evidence cannot be later than generation")
        if self.valid_until <= self.generated_at:
            raise ValueError("valid_until must be after generated_at")

        if self.grade is SetupGrade.REJECT:
            self._validate_rejected()
        else:
            self._validate_tradeable()

    def _validate_rejected(self) -> None:
        if not self.blockers:
            raise ValueError("REJECT decision requires a blocker")
        if self.risk_fraction != 0:
            raise ValueError("REJECT decision requires zero risk")
        if self.calculated_quantity != 0:
            raise ValueError("REJECT decision requires zero quantity")
        if any(
            value is not None
            for value in (
                self.planned_entry,
                self.structural_stop,
                self.target_price,
                self.expected_cost,
            )
        ):
            raise ValueError("REJECT decision cannot carry order terms")

    def _validate_tradeable(self) -> None:
        if self.blockers:
            raise ValueError("tradeable decision cannot carry blockers")
        if self.risk_fraction <= 0:
            raise ValueError("tradeable decision requires positive risk")
        if self.calculated_quantity <= 0:
            raise ValueError("tradeable decision requires positive quantity")
        if self.expected_cost is None or self.expected_cost < 0:
            raise ValueError("tradeable decision requires non-negative expected cost")
        if (
            self.planned_entry is None
            or self.structural_stop is None
            or self.target_price is None
            or min(self.planned_entry, self.structural_stop, self.target_price) <= 0
        ):
            raise ValueError("tradeable decision prices must be positive")
        if self.side is Side.BUY and not (
            self.structural_stop < self.planned_entry < self.target_price
        ):
            raise ValueError("BUY decision requires stop below entry below target")
        if self.side is Side.SELL and not (
            self.target_price < self.planned_entry < self.structural_stop
        ):
            raise ValueError("SELL decision requires target below entry below stop")

    def decision_hash(self) -> bytes:
        return canonical_v6_hash(self)


def canonical_v6_hash(value: object) -> bytes:
    return hashlib.sha256(
        json.dumps(
            _canonical_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).digest()


def _require_canonical_indicators(
    indicators: tuple[MatchedIndicator, ...],
) -> None:
    if type(indicators) is not tuple or any(
        type(indicator) is not MatchedIndicator for indicator in indicators
    ):
        raise TypeError("matched_indicators must be a tuple of MatchedIndicator")
    keys = tuple(indicator.key for indicator in indicators)
    if keys != tuple(sorted(keys)):
        raise ValueError("matched indicator keys must be sorted")
    if len(set(keys)) != len(keys):
        raise ValueError("matched indicator keys must be unique")


def _require_sorted_unique_text(values: tuple[str, ...], name: str) -> None:
    if type(values) is not tuple or any(
        type(value) is not str or not value or value.strip() != value
        for value in values
    ):
        raise TypeError(f"{name} must contain non-empty strings")
    if values != tuple(sorted(values)):
        raise ValueError(f"{name} must be sorted")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must be unique")


def _require_canonical_hashes(values: tuple[bytes, ...]) -> None:
    if type(values) is not tuple or not values:
        raise ValueError("source_evidence_hashes must be a non-empty tuple")
    for value in values:
        _require_sha256(value, "source evidence hash")
    if values != tuple(sorted(values)):
        raise ValueError("source_evidence_hashes must be sorted")
    if len(set(values)) != len(values):
        raise ValueError("source_evidence_hashes must be unique")


def _require_sha256(value: object, name: str) -> None:
    if type(value) is not bytes or len(value) != 32:
        raise TypeError(f"{name} must be SHA-256 bytes")


def _require_uuid7(value: object, name: str) -> None:
    if not isinstance(value, UUID) or value.version != 7:
        raise ValueError(f"{name} must be UUIDv7")


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return require_decimal(value)


def _canonical_decimal(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")


def _canonical_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in cast(tuple[object, ...], value)]
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if type(value) is bytes:
        return cast(bytes, value).hex()
    if value is None or type(value) in {str, int, bool}:
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")
