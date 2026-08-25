from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

_HASH_VERSION = b"TOSS_HLIT_MARKET_SAFETY_SOURCE_V1"


@dataclass(frozen=True, slots=True)
class TossHlitKrxMarketSafetyEvidence:
    """Validated scalar Toss market-safety facts without broker dependencies."""

    symbol: str
    observed_at: datetime
    has_active_krx_vi: bool
    is_single_price_auction: bool

    def __post_init__(self) -> None:
        if not _is_valid_symbol(self.symbol):
            raise ValueError("Toss HLIT market-safety symbol is invalid")
        if not _is_exact_utc(self.observed_at):
            raise ValueError("Toss HLIT market-safety observation must use exact UTC")
        if type(self.has_active_krx_vi) is not bool:
            raise ValueError("Toss HLIT market-safety KRX VI fact is invalid")
        if type(self.is_single_price_auction) is not bool:
            raise ValueError("Toss HLIT market-safety auction fact is invalid")


@dataclass(frozen=True, slots=True)
class TossHlitKrxMarketSafetySourceEvidence:
    """Scalar safety evidence bound to exact decoded Toss source snapshots."""

    evidence: TossHlitKrxMarketSafetyEvidence
    vi_source_id: UUID
    vi_source_hash: bytes
    vi_expires_at: datetime
    calendar_source_id: UUID
    calendar_source_hash: bytes
    calendar_expires_at: datetime
    source_hash: bytes

    def __post_init__(self) -> None:
        if type(self.evidence) is not TossHlitKrxMarketSafetyEvidence:
            raise ValueError("Toss HLIT source scalar evidence is invalid")
        self.evidence.__post_init__()
        _require_source_id(self.vi_source_id, "VI source ID")
        _require_digest(self.vi_source_hash, "VI source hash")
        _require_expiry(
            self.vi_expires_at,
            observed_at=self.evidence.observed_at,
            name="VI expiry",
        )
        _require_source_id(self.calendar_source_id, "calendar source ID")
        _require_digest(self.calendar_source_hash, "calendar source hash")
        _require_expiry(
            self.calendar_expires_at,
            observed_at=self.evidence.observed_at,
            name="calendar expiry",
        )
        _require_digest(self.source_hash, "source hash")
        if self.source_hash != _source_hash(
            evidence=self.evidence,
            vi_source_id=self.vi_source_id,
            vi_source_hash=self.vi_source_hash,
            vi_expires_at=self.vi_expires_at,
            calendar_source_id=self.calendar_source_id,
            calendar_source_hash=self.calendar_source_hash,
            calendar_expires_at=self.calendar_expires_at,
        ):
            raise ValueError("source hash must bind all Toss safety source evidence")

    @classmethod
    def from_components(
        cls,
        *,
        evidence: TossHlitKrxMarketSafetyEvidence,
        vi_source_id: UUID,
        vi_source_hash: bytes,
        vi_expires_at: datetime,
        calendar_source_id: UUID,
        calendar_source_hash: bytes,
        calendar_expires_at: datetime,
    ) -> TossHlitKrxMarketSafetySourceEvidence:
        source_hash = _source_hash(
            evidence=evidence,
            vi_source_id=vi_source_id,
            vi_source_hash=vi_source_hash,
            vi_expires_at=vi_expires_at,
            calendar_source_id=calendar_source_id,
            calendar_source_hash=calendar_source_hash,
            calendar_expires_at=calendar_expires_at,
        )
        return cls(
            evidence=evidence,
            vi_source_id=vi_source_id,
            vi_source_hash=vi_source_hash,
            vi_expires_at=vi_expires_at,
            calendar_source_id=calendar_source_id,
            calendar_source_hash=calendar_source_hash,
            calendar_expires_at=calendar_expires_at,
            source_hash=source_hash,
        )


def _is_valid_symbol(value: object) -> bool:
    return (
        type(value) is str and len(value) == 6 and value.isascii() and value.isdigit()
    )


def _is_exact_utc(value: object) -> bool:
    return (
        type(value) is datetime
        and value.tzinfo is UTC
        and value.utcoffset() == timedelta(0)
        and value.microsecond == 0
    )


def _source_hash(
    *,
    evidence: TossHlitKrxMarketSafetyEvidence,
    vi_source_id: UUID,
    vi_source_hash: bytes,
    vi_expires_at: datetime,
    calendar_source_id: UUID,
    calendar_source_hash: bytes,
    calendar_expires_at: datetime,
) -> bytes:
    evidence.__post_init__()
    _require_source_id(vi_source_id, "VI source ID")
    _require_digest(vi_source_hash, "VI source hash")
    _require_expiry(vi_expires_at, observed_at=evidence.observed_at, name="VI expiry")
    _require_source_id(calendar_source_id, "calendar source ID")
    _require_digest(calendar_source_hash, "calendar source hash")
    _require_expiry(
        calendar_expires_at,
        observed_at=evidence.observed_at,
        name="calendar expiry",
    )
    return _hash_values(
        b"SOURCE",
        evidence.symbol.encode("ascii"),
        _datetime_bytes(evidence.observed_at),
        b"1" if evidence.has_active_krx_vi else b"0",
        b"1" if evidence.is_single_price_auction else b"0",
        vi_source_id.bytes,
        vi_source_hash,
        _datetime_bytes(vi_expires_at),
        calendar_source_id.bytes,
        calendar_source_hash,
        _datetime_bytes(calendar_expires_at),
    )


def _hash_values(tag: bytes, *values: bytes) -> bytes:
    payload = bytearray()
    for value in (_HASH_VERSION, tag, *values):
        payload.extend(len(value).to_bytes(8, "big"))
        payload.extend(value)
    return hashlib.sha256(bytes(payload)).digest()


def _datetime_bytes(value: datetime) -> bytes:
    if not _is_exact_utc(value):
        raise ValueError("canonical Toss source time must use whole-second UTC")
    return value.strftime("%Y-%m-%dT%H:%M:%SZ").encode("ascii")


def _require_source_id(value: object, name: str) -> UUID:
    if type(value) is not UUID or value.int == 0:
        raise ValueError(f"{name} must be a nonzero UUID")
    return value


def _require_digest(value: object, name: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        raise ValueError(f"{name} must be exactly 32 bytes")
    return value


def _require_expiry(value: object, *, observed_at: datetime, name: str) -> datetime:
    if type(value) is not datetime or not _is_exact_utc(value):
        raise ValueError(f"{name} must be exclusive whole-second UTC")
    if value <= observed_at:
        raise ValueError(f"{name} must be exclusive whole-second UTC")
    return value
