from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, date, datetime, timedelta
from typing import NoReturn, cast
from uuid import UUID

from autotrader.domain.toss_hlit_market_safety import (
    TossHlitKrxMarketSafetyEvidence,
    TossHlitKrxMarketSafetySourceEvidence,
)
from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerWriteDisabled,
)
from autotrader.integrations.brokers.toss.domestic_vi import (
    TossDomesticViReadOnlyAdapter,
    TossKrxViEvidence,
    TossKrxViWarning,
)
from autotrader.integrations.brokers.toss.kr_market_calendar import (
    TossKrMarketCalendar,
    TossKrMarketCalendarDay,
    TossKrMarketCalendarReadOnlyAdapter,
    TossKrSinglePriceAuctionWindow,
    is_toss_kr_single_price_auction,
)

_KST_OFFSET = timedelta(hours=9)
_INCOMPLETE = "Toss HLIT market-safety snapshot is incomplete"
_WRITE_DISABLED = "Toss HLIT market-safety snapshot write adapter is not enabled"
_SOURCE_HASH_VERSION = b"TOSS_HLIT_PROVIDER_COMPONENT_V1"


class TossIncompleteHlitKrxMarketSafetySnapshot(RuntimeError):
    """Raised when Toss facts cannot form a complete market-safety snapshot."""


def build_toss_hlit_krx_market_safety_evidence(
    *, symbol: object, observed_at: object, vi_evidence: object, calendar: object
) -> TossHlitKrxMarketSafetyEvidence:
    """Compose canonical Toss VI and calendar facts without provider access."""

    if not _is_valid_symbol(symbol):
        raise ValueError("Toss HLIT market-safety symbol is invalid")
    if not _is_exact_utc(observed_at):
        raise ValueError("Toss HLIT market-safety observation must use exact UTC")
    if type(vi_evidence) is not TossKrxViEvidence:
        raise ValueError("Toss HLIT market-safety VI evidence is invalid")
    if type(calendar) is not TossKrMarketCalendar:
        raise ValueError("Toss HLIT market-safety calendar is invalid")

    resolved_observed_at = cast(datetime, observed_at)
    if not _is_kst_collection_time_representable(resolved_observed_at):
        raise ValueError("Toss HLIT market-safety observation is out of KST range")
    resolved_calendar = calendar
    observed_calendar_date = (resolved_observed_at + _KST_OFFSET).date()
    if observed_calendar_date != resolved_calendar.today.calendar_date:
        raise ValueError("Toss HLIT market-safety calendar date is invalid")

    resolved_vi_evidence = vi_evidence
    return TossHlitKrxMarketSafetyEvidence(
        symbol=cast(str, symbol),
        observed_at=resolved_observed_at,
        has_active_krx_vi=resolved_vi_evidence.has_active_krx_vi,
        is_single_price_auction=is_toss_kr_single_price_auction(
            calendar=resolved_calendar, observed_at=resolved_observed_at
        ),
    )


def build_toss_hlit_krx_market_safety_source_evidence(
    *,
    symbol: object,
    observed_at: object,
    vi_evidence: object,
    calendar: object,
    vi_source_id: object,
    vi_expires_at: object,
    calendar_source_id: object,
    calendar_expires_at: object,
) -> TossHlitKrxMarketSafetySourceEvidence:
    evidence = build_toss_hlit_krx_market_safety_evidence(
        symbol=symbol,
        observed_at=observed_at,
        vi_evidence=vi_evidence,
        calendar=calendar,
    )
    if type(vi_source_id) is not UUID or vi_source_id.int == 0:
        raise ValueError("Toss HLIT VI source ID is invalid")
    if type(calendar_source_id) is not UUID or calendar_source_id.int == 0:
        raise ValueError("Toss HLIT calendar source ID is invalid")
    if type(vi_expires_at) is not datetime:
        raise ValueError("Toss HLIT VI expiry is invalid")
    if type(calendar_expires_at) is not datetime:
        raise ValueError("Toss HLIT calendar expiry is invalid")
    resolved_vi = cast(TossKrxViEvidence, vi_evidence)
    resolved_calendar = cast(TossKrMarketCalendar, calendar)
    resolved_vi.__post_init__()
    resolved_calendar.__post_init__()
    return TossHlitKrxMarketSafetySourceEvidence.from_components(
        evidence=evidence,
        vi_source_id=vi_source_id,
        vi_source_hash=_vi_hash(resolved_vi),
        vi_expires_at=vi_expires_at,
        calendar_source_id=calendar_source_id,
        calendar_source_hash=_calendar_hash(resolved_calendar),
        calendar_expires_at=calendar_expires_at,
    )


class TossHlitKrxMarketSafetyReadOnlyObserver:
    """Collects Toss market-safety evidence without account or write capability."""

    def __init__(self, *, transport: AsyncHttpTransport) -> None:
        self._transport = transport

    async def read_snapshot(
        self, *, symbol: object, observed_at: object, access_token: object
    ) -> TossHlitKrxMarketSafetyEvidence:
        transport = self._transport
        try:
            evidence = await _read_snapshot(
                transport=transport,
                symbol=symbol,
                observed_at=observed_at,
                access_token=access_token,
            )
        finally:
            del self, transport, symbol, observed_at, access_token
        if evidence is None:
            raise TossIncompleteHlitKrxMarketSafetySnapshot(_INCOMPLETE)
        return evidence

    async def submit(self, *, command: object) -> NoReturn:
        del self, command
        raise BrokerWriteDisabled(_WRITE_DISABLED)

    async def cancel(self, *, command: object) -> NoReturn:
        del self, command
        raise BrokerWriteDisabled(_WRITE_DISABLED)

    async def replace(self, *, command: object) -> NoReturn:
        del self, command
        raise BrokerWriteDisabled(_WRITE_DISABLED)


async def _read_snapshot(
    *,
    transport: AsyncHttpTransport,
    symbol: object,
    observed_at: object,
    access_token: object,
) -> TossHlitKrxMarketSafetyEvidence | None:
    vi_adapter: TossDomesticViReadOnlyAdapter | None = None
    calendar_adapter: TossKrMarketCalendarReadOnlyAdapter | None = None
    vi_evidence: TossKrxViEvidence | None = None
    calendar: TossKrMarketCalendar | None = None
    observed: datetime | None = None
    try:
        if (
            not _is_valid_symbol(symbol)
            or not _is_exact_utc(observed_at)
            or not _is_kst_collection_time_representable(cast(datetime, observed_at))
        ):
            return None
        observed = cast(datetime, observed_at)
        vi_adapter = TossDomesticViReadOnlyAdapter(transport=transport)
        vi_evidence = await vi_adapter.read_krx_vi_evidence(
            symbol=symbol, access_token=access_token
        )
        calendar_adapter = TossKrMarketCalendarReadOnlyAdapter(transport=transport)
        calendar = await calendar_adapter.read_kr_market_calendar(
            access_token=access_token,
            calendar_date=(observed + _KST_OFFSET).date(),
        )
        return build_toss_hlit_krx_market_safety_evidence(
            symbol=symbol,
            observed_at=observed,
            vi_evidence=vi_evidence,
            calendar=calendar,
        )
    except asyncio.CancelledError, Exception:
        return None
    finally:
        del (
            transport,
            symbol,
            observed_at,
            access_token,
            vi_adapter,
            calendar_adapter,
            vi_evidence,
            calendar,
            observed,
        )


def _is_valid_symbol(value: object) -> bool:
    if type(value) is not str:
        return False
    return len(value) == 6 and value.isascii() and value.isdigit()


def _is_exact_utc(value: object) -> bool:
    if type(value) is not datetime:
        return False
    observed_at = value
    return observed_at.tzinfo is UTC and observed_at.utcoffset() == timedelta(0)


def _is_kst_collection_time_representable(observed_at: datetime) -> bool:
    return observed_at <= datetime.max.replace(tzinfo=UTC) - _KST_OFFSET


def _vi_hash(evidence: TossKrxViEvidence) -> bytes:
    evidence.__post_init__()
    records: list[bytes] = []
    for warning in evidence.warnings:
        warning.__post_init__()
        records.append(_warning_bytes(warning))
    return _component_hash(b"VI", *records)


def _warning_bytes(warning: TossKrxViWarning) -> bytes:
    return _encode_values(
        warning.warning_type.encode("utf-8"),
        _optional_text(warning.exchange),
        _optional_date(warning.start_date),
        _optional_date(warning.end_date),
    )


def _calendar_hash(calendar: TossKrMarketCalendar) -> bytes:
    calendar.__post_init__()
    return _component_hash(
        b"CALENDAR",
        _calendar_day_bytes(calendar.previous_business_day),
        _calendar_day_bytes(calendar.today),
        _calendar_day_bytes(calendar.next_business_day),
    )


def _calendar_day_bytes(day: TossKrMarketCalendarDay) -> bytes:
    day.__post_init__()
    windows: list[bytes] = []
    for window in day.windows:
        window.__post_init__()
        windows.append(_window_bytes(window))
    return _encode_values(day.calendar_date.isoformat().encode("ascii"), *windows)


def _window_bytes(window: TossKrSinglePriceAuctionWindow) -> bytes:
    return _encode_values(
        _utc_bytes(window.start_at),
        _utc_bytes(window.end_at),
    )


def _component_hash(tag: bytes, *values: bytes) -> bytes:
    return hashlib.sha256(_encode_values(_SOURCE_HASH_VERSION, tag, *values)).digest()


def _encode_values(*values: bytes) -> bytes:
    payload = bytearray()
    for value in values:
        payload.extend(len(value).to_bytes(8, "big"))
        payload.extend(value)
    return bytes(payload)


def _optional_text(value: str | None) -> bytes:
    if value is None:
        return b"N"
    return b"S" + value.encode("utf-8")


def _optional_date(value: date | None) -> bytes:
    if value is None:
        return b"N"
    return b"D" + value.isoformat().encode("ascii")


def _utc_bytes(value: datetime) -> bytes:
    if not _is_exact_utc(value) or value.microsecond != 0:
        raise ValueError("Toss HLIT provider time must use whole-second UTC")
    return value.strftime("%Y-%m-%dT%H:%M:%SZ").encode("ascii")
