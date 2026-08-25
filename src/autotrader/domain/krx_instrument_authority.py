from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Self, cast
from unicodedata import category
from uuid import UUID

_KST = timezone(timedelta(hours=9))


class KrxCashMarket(StrEnum):
    KOSPI = "KOSPI"
    KOSDAQ = "KOSDAQ"


@dataclass(frozen=True, slots=True)
class KrxInstrumentIdentityBinding:
    symbol: str
    instrument_id: UUID

    def __post_init__(self) -> None:
        if not _is_six_ascii_digits(self.symbol):
            raise ValueError("symbol must be exactly six ASCII digits")
        _require_uuid7(self.instrument_id, "instrument_id")


@dataclass(frozen=True, slots=True)
class KrxCommonStockInstrumentAuthority:
    market: KrxCashMarket
    symbol: str
    standard_code: str
    name: str
    security_group_code: str
    etp_product_class_code: str
    preferred_stock_class_code: str

    def __post_init__(self) -> None:
        if type(self.market) is not KrxCashMarket:
            raise ValueError("market must be an exact KrxCashMarket")
        if not _is_six_ascii_digits(self.symbol):
            raise ValueError("symbol must be exactly six ASCII digits")
        if not _is_standard_code(self.standard_code):
            raise ValueError("standard_code must be 12 uppercase ASCII alphanumerics")
        _require_name(self.name)
        if type(self.security_group_code) is not str:
            raise ValueError("security_group_code must be an exact str")
        if self.security_group_code != "ST":
            raise ValueError("security_group_code must be ST")
        if type(self.etp_product_class_code) is not str:
            raise ValueError("etp_product_class_code must be an exact str")
        if self.etp_product_class_code not in {"", "0"}:
            raise ValueError("etp_product_class_code must identify a non-ETP")
        if type(self.preferred_stock_class_code) is not str:
            raise ValueError("preferred_stock_class_code must be an exact str")
        if self.preferred_stock_class_code != "0":
            raise ValueError("preferred_stock_class_code must identify common stock")


@dataclass(frozen=True, slots=True)
class KrxCommonStockAuthoritySnapshot:
    captured_at: datetime
    kospi_source_hash: bytes
    kosdaq_source_hash: bytes
    instruments: tuple[KrxCommonStockInstrumentAuthority, ...]
    source_hash: bytes

    def __post_init__(self) -> None:
        _require_whole_second_utc(self.captured_at)
        _require_sha256(self.kospi_source_hash, "kospi_source_hash")
        _require_sha256(self.kosdaq_source_hash, "kosdaq_source_hash")
        _validate_instruments(self.instruments)
        _require_sha256(self.source_hash, "source_hash")
        if self.source_hash != _snapshot_hash(
            captured_at=self.captured_at,
            kospi_source_hash=self.kospi_source_hash,
            kosdaq_source_hash=self.kosdaq_source_hash,
            instruments=self.instruments,
        ):
            raise ValueError("source_hash does not match the authority projection")

    @classmethod
    def build(
        cls,
        *,
        captured_at: datetime,
        kospi_source_hash: bytes,
        kosdaq_source_hash: bytes,
        instruments: tuple[KrxCommonStockInstrumentAuthority, ...],
    ) -> Self:
        _require_whole_second_utc(captured_at)
        _require_sha256(kospi_source_hash, "kospi_source_hash")
        _require_sha256(kosdaq_source_hash, "kosdaq_source_hash")
        _validate_instruments(instruments)
        return cls(
            captured_at=captured_at,
            kospi_source_hash=kospi_source_hash,
            kosdaq_source_hash=kosdaq_source_hash,
            instruments=instruments,
            source_hash=_snapshot_hash(
                captured_at=captured_at,
                kospi_source_hash=kospi_source_hash,
                kosdaq_source_hash=kosdaq_source_hash,
                instruments=instruments,
            ),
        )


@dataclass(frozen=True, slots=True)
class KrxAuthorityActivationManifest:
    snapshot_id: UUID
    source_hash: bytes
    source_last_modified_at: datetime
    trading_date: date
    activated_at: datetime
    valid_from: datetime
    valid_until: datetime
    calendar_evidence_hash: bytes
    activation_hash: bytes

    def __post_init__(self) -> None:
        _require_uuid7(self.snapshot_id, "snapshot_id")
        _require_sha256(self.source_hash, "source_hash")
        _require_whole_second_utc(
            self.source_last_modified_at, "source_last_modified_at"
        )
        if type(self.trading_date) is not date:
            raise ValueError("trading_date must be an exact date")
        _require_whole_second_utc(self.activated_at, "activated_at")
        _require_whole_second_utc(self.valid_from, "valid_from")
        _require_whole_second_utc(self.valid_until, "valid_until")
        _require_sha256(self.calendar_evidence_hash, "calendar_evidence_hash")
        _require_sha256(self.activation_hash, "activation_hash")
        if self.source_last_modified_at.astimezone(_KST).date() != self.trading_date:
            raise ValueError("source time does not match trading_date")
        if self.activated_at.astimezone(_KST).date() != self.trading_date:
            raise ValueError("activation time does not match trading_date")
        if self.activated_at < self.source_last_modified_at:
            raise ValueError("activated_at must not predate source time")
        if self.valid_from != self.activated_at:
            raise ValueError("valid_from must equal activated_at")
        if self.valid_until != _next_kst_midnight(self.trading_date):
            raise ValueError("valid_until must be the next KST midnight")
        if self.activation_hash != _activation_hash(
            snapshot_id=self.snapshot_id,
            source_hash=self.source_hash,
            source_last_modified_at=self.source_last_modified_at,
            trading_date=self.trading_date,
            activated_at=self.activated_at,
            valid_from=self.valid_from,
            valid_until=self.valid_until,
            calendar_evidence_hash=self.calendar_evidence_hash,
        ):
            raise ValueError("activation_hash does not match activation evidence")


@dataclass(frozen=True, slots=True)
class ActivatedKrxCommonStockAuthority:
    snapshot: KrxCommonStockAuthoritySnapshot
    activation_id: UUID
    trading_date: date
    valid_from: datetime
    valid_until: datetime
    activation_hash: bytes

    def __post_init__(self) -> None:
        if type(self.snapshot) is not KrxCommonStockAuthoritySnapshot:
            raise ValueError("snapshot must be the exact KRX authority type")
        try:
            self.snapshot.__post_init__()
        except (TypeError, ValueError) as error:
            raise ValueError("snapshot invariant is invalid") from error
        _require_uuid7(self.activation_id, "activation_id")
        if type(self.trading_date) is not date:
            raise ValueError("trading_date must be an exact date")
        _require_whole_second_utc(self.valid_from, "valid_from")
        _require_whole_second_utc(self.valid_until, "valid_until")
        _require_sha256(self.activation_hash, "activation_hash")
        if self.snapshot.captured_at.astimezone(_KST).date() != self.trading_date:
            raise ValueError("snapshot date does not match activation")
        if self.valid_until != _next_kst_midnight(self.trading_date):
            raise ValueError("valid_until must be the next KST midnight")
        if not self.valid_from < self.valid_until:
            raise ValueError("activation validity must be non-empty")


def prepare_krx_authority_activation(
    *,
    snapshot_id: UUID,
    snapshot: KrxCommonStockAuthoritySnapshot,
    calendar_evidence_hash: bytes,
    requested_date: date,
    activated_at: datetime,
) -> KrxAuthorityActivationManifest:
    if type(snapshot) is not KrxCommonStockAuthoritySnapshot:
        raise ValueError("exact KRX authority snapshot is required")
    snapshot.__post_init__()
    _require_uuid7(snapshot_id, "snapshot_id")
    _require_sha256(calendar_evidence_hash, "calendar_evidence_hash")
    if type(requested_date) is not date:
        raise ValueError("requested_date must be an exact date")
    _require_whole_second_utc(activated_at, "activated_at")
    if snapshot.captured_at.astimezone(_KST).date() != requested_date:
        raise ValueError("source date does not match requested date")
    if activated_at.astimezone(_KST).date() != requested_date:
        raise ValueError("activation date does not match requested date")
    if activated_at < snapshot.captured_at:
        raise ValueError("activated_at must not predate source time")
    valid_until = _next_kst_midnight(requested_date)
    activation_hash = _activation_hash(
        snapshot_id=snapshot_id,
        source_hash=snapshot.source_hash,
        source_last_modified_at=snapshot.captured_at,
        trading_date=requested_date,
        activated_at=activated_at,
        valid_from=activated_at,
        valid_until=valid_until,
        calendar_evidence_hash=calendar_evidence_hash,
    )
    return KrxAuthorityActivationManifest(
        snapshot_id=snapshot_id,
        source_hash=snapshot.source_hash,
        source_last_modified_at=snapshot.captured_at,
        trading_date=requested_date,
        activated_at=activated_at,
        valid_from=activated_at,
        valid_until=valid_until,
        calendar_evidence_hash=calendar_evidence_hash,
        activation_hash=activation_hash,
    )


def _is_six_ascii_digits(value: object) -> bool:
    return (
        type(value) is str and len(value) == 6 and value.isascii() and value.isdigit()
    )


def _is_standard_code(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 12
        and value.isascii()
        and value.isalnum()
        and value == value.upper()
    )


def _require_name(value: object) -> None:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(category(character) == "Cc" for character in value)
    ):
        raise ValueError("name must be a trimmed non-empty control-free exact str")


def _require_whole_second_utc(value: object, field: str = "captured_at") -> None:
    if type(value) is not datetime or value.tzinfo is not UTC or value.microsecond != 0:
        raise ValueError(f"{field} must be a whole-second exact UTC datetime")


def _require_sha256(value: object, field: str) -> None:
    if type(value) is not bytes or len(value) != 32:
        raise ValueError(f"{field} must be an exact SHA-256 bytes value")


def _require_uuid7(value: object, field: str) -> None:
    if type(value) is not UUID or value.version != 7:
        raise ValueError(f"{field} must be an exact UUIDv7")


def _next_kst_midnight(trading_date: date) -> datetime:
    return datetime.combine(
        trading_date + timedelta(days=1), time.min, tzinfo=_KST
    ).astimezone(UTC)


def _activation_hash(
    *,
    snapshot_id: UUID,
    source_hash: bytes,
    source_last_modified_at: datetime,
    trading_date: date,
    activated_at: datetime,
    valid_from: datetime,
    valid_until: datetime,
    calendar_evidence_hash: bytes,
) -> bytes:
    digest = sha256()
    for value in (
        b"KIS_KRX_COMMON_STOCK_AUTHORITY_ACTIVATION_V1",
        snapshot_id.bytes,
        source_hash,
        source_last_modified_at.isoformat().encode("ascii"),
        trading_date.isoformat().encode("ascii"),
        activated_at.isoformat().encode("ascii"),
        valid_from.isoformat().encode("ascii"),
        valid_until.isoformat().encode("ascii"),
        calendar_evidence_hash,
    ):
        digest.update(len(value).to_bytes(4, "big"))
        digest.update(value)
    return digest.digest()


def _validate_instruments(value: object) -> None:
    if type(value) is not tuple or not value:
        raise ValueError("instruments must be a non-empty exact tuple")
    symbols: list[str] = []
    for instrument in cast(tuple[object, ...], value):
        if type(instrument) is not KrxCommonStockInstrumentAuthority:
            raise ValueError("instrument must have the exact authority type")
        try:
            instrument.__post_init__()
        except (TypeError, ValueError) as error:
            raise ValueError("instrument invariant is invalid") from error
        symbols.append(instrument.symbol)
    if symbols != sorted(symbols) or len(symbols) != len(set(symbols)):
        raise ValueError("instruments must have unique symbol ordering")


def _snapshot_hash(
    *,
    captured_at: datetime,
    kospi_source_hash: bytes,
    kosdaq_source_hash: bytes,
    instruments: tuple[KrxCommonStockInstrumentAuthority, ...],
) -> bytes:
    digest = sha256()
    values = [
        b"KIS_KRX_COMMON_STOCK_AUTHORITY_V2",
        captured_at.isoformat().encode("ascii"),
        kospi_source_hash,
        kosdaq_source_hash,
    ]
    for instrument in instruments:
        values.extend(
            (
                instrument.market.value.encode("ascii"),
                instrument.symbol.encode("ascii"),
                instrument.standard_code.encode("ascii"),
                instrument.name.encode("utf-8"),
                instrument.security_group_code.encode("ascii"),
                instrument.etp_product_class_code.encode("ascii"),
                instrument.preferred_stock_class_code.encode("ascii"),
            )
        )
    for value in values:
        digest.update(len(value).to_bytes(4, "big"))
        digest.update(value)
    return digest.digest()
