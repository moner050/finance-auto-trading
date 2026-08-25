from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.strategies.david_v6.models import EvidenceState, V6Market

TIMEFRAMES = {
    "5s": timedelta(seconds=5),
    "30s": timedelta(seconds=30),
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
}
_CASH_FORBIDDEN_TIMEFRAMES = {"5s", "30s"}


@dataclass(frozen=True, slots=True)
class EvidenceProvenance:
    source: str
    source_key: str
    source_timezone: str
    observed_at: datetime
    captured_at: datetime
    digest_sha256: str

    def __post_init__(self) -> None:
        _require_text(self.source, "source")
        _require_text(self.source_key, "source_key")
        _require_source_timezone(self.source_timezone)
        observed_at = _normalize_utc(self.observed_at, "observed_at")
        captured_at = _normalize_utc(self.captured_at, "captured_at")
        if captured_at < observed_at:
            raise ValueError("captured_at cannot precede observed_at")
        if not _is_sha256_hex(self.digest_sha256):
            raise ValueError("digest_sha256 must be canonical SHA-256 hex")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "captured_at", captured_at)


@dataclass(frozen=True, slots=True)
class EvidenceItem[T]:
    state: EvidenceState
    value: T | None
    provenance: EvidenceProvenance | None
    blocker_code: str | None

    def __post_init__(self) -> None:
        if type(self.state) is not EvidenceState:
            raise TypeError("state must be an exact EvidenceState")
        if (
            self.provenance is not None
            and type(self.provenance) is not EvidenceProvenance
        ):
            raise TypeError("provenance must be exact EvidenceProvenance")
        if self.blocker_code is not None:
            _require_text(self.blocker_code, "blocker_code")
        if self.value is None and self.provenance is not None:
            raise ValueError("provenance cannot exist without a value")
        if self.value is not None and self.provenance is None:
            raise ValueError("a value requires provenance")
        if self.state is EvidenceState.AVAILABLE:
            if self.value is None:
                raise ValueError("AVAILABLE evidence requires a value and provenance")
            if self.blocker_code is not None:
                raise ValueError("AVAILABLE evidence cannot carry a blocker")
            return
        if self.value is not None:
            raise ValueError("non-AVAILABLE evidence cannot expose a value")


@dataclass(frozen=True, slots=True)
class V6EvidenceBundle:
    market: V6Market
    instrument_id: UUID
    decision_at: datetime
    bars: Mapping[str, EvidenceItem[tuple[CompletedOhlcvBar, ...]]]
    universe: EvidenceItem[object]
    regime: EvidenceItem[object]
    metodo: EvidenceItem[object]
    zones: EvidenceItem[object]
    divergence: EvidenceItem[object]
    exhaustion: EvidenceItem[object]
    order_flow: EvidenceItem[object]
    profile: EvidenceItem[object]
    calendar: EvidenceItem[object]
    session: EvidenceItem[object]
    costs: EvidenceItem[object]

    def __post_init__(self) -> None:
        if type(cast(object, self.market)) is not V6Market:
            raise TypeError("market must be an exact V6Market")
        instrument_id = cast(object, self.instrument_id)
        if not isinstance(instrument_id, UUID) or instrument_id.version != 7:
            raise ValueError("instrument_id must be UUIDv7")
        decision_at = _normalize_utc(self.decision_at, "decision_at")
        raw_bars = cast(object, self.bars)
        if not isinstance(raw_bars, Mapping):
            raise TypeError("bars must be a mapping")
        bar_items = dict(self.bars)
        if any(type(key) is not str or key not in TIMEFRAMES for key in bar_items):
            raise ValueError("bars contain an unsupported timeframe key")
        if self.market in {V6Market.KRX_CASH, V6Market.US_CASH} and any(
            key in _CASH_FORBIDDEN_TIMEFRAMES for key in bar_items
        ):
            raise ValueError(
                "cash bundles cannot contain 30-second or 5-second evidence"
            )
        for key, item in bar_items.items():
            _require_bar_item(
                item=item, timeframe=TIMEFRAMES[key], decision_at=decision_at
            )
        for item in self._fact_items():
            if type(item) is not EvidenceItem:
                raise TypeError("bundle facts must be exact EvidenceItem values")
            item.__post_init__()
        object.__setattr__(self, "decision_at", decision_at)
        object.__setattr__(
            self,
            "bars",
            MappingProxyType(dict(sorted(bar_items.items()))),
        )

    def _fact_items(self) -> tuple[EvidenceItem[object], ...]:
        return (
            self.universe,
            self.regime,
            self.metodo,
            self.zones,
            self.divergence,
            self.exhaustion,
            self.order_flow,
            self.profile,
            self.calendar,
            self.session,
            self.costs,
        )


def _require_bar_item(
    *,
    item: object,
    timeframe: timedelta,
    decision_at: datetime,
) -> None:
    if type(item) is not EvidenceItem:
        raise TypeError("bar evidence must be an exact EvidenceItem")
    typed_item = cast(EvidenceItem[object], item)
    typed_item.__post_init__()
    if typed_item.value is None:
        return
    raw_value = typed_item.value
    if type(raw_value) is not tuple or any(
        type(bar) is not CompletedOhlcvBar
        for bar in cast(tuple[object, ...], raw_value)
    ):
        raise TypeError("available bar evidence must be completed OHLCV tuple")
    bars = cast(tuple[CompletedOhlcvBar, ...], raw_value)
    if any(bar.timestamp + timeframe > decision_at for bar in bars):
        raise ValueError("bar evidence cannot contain a forming bar")


def _normalize_utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware instant")
    return value.astimezone(UTC)


def _require_text(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError(f"{name} must be non-empty single-line text")


def _require_source_timezone(value: object) -> None:
    _require_text(value, "source_timezone")
    try:
        ZoneInfo(cast(str, value))
    except ZoneInfoNotFoundError as error:
        raise ValueError("source_timezone must be an IANA timezone") from error


def _is_sha256_hex(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = (
    "EvidenceItem",
    "EvidenceProvenance",
    "V6EvidenceBundle",
)
