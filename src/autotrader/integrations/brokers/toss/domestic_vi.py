from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import NoReturn, cast

from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)

_INCOMPLETE = "Toss KRX VI snapshot is incomplete"
_INVALID_SYMBOL = "Toss KRX VI symbol is invalid"
_INVALID_TOKEN = "Toss KRX VI access token is invalid"
_VI_WARNING_TYPES = frozenset({"VI_STATIC", "VI_DYNAMIC", "VI_STATIC_AND_DYNAMIC"})


class TossIncompleteKrxViSnapshot(RuntimeError):
    """Raised when Toss VI warnings cannot form a complete provider snapshot."""


@dataclass(frozen=True, slots=True)
class TossKrxViWarning:
    """One Toss warning record retained in its provider response order."""

    warning_type: str
    exchange: str | None
    start_date: date | None
    end_date: date | None

    def __post_init__(self) -> None:
        if not _single_line_text(self.warning_type):
            raise ValueError("Toss KRX VI warning type is invalid")
        if self.exchange is not None and not _single_line_text(self.exchange):
            raise ValueError("Toss KRX VI warning exchange is invalid")
        if (self.start_date is not None and type(self.start_date) is not date) or (
            self.end_date is not None and type(self.end_date) is not date
        ):
            raise ValueError("Toss KRX VI warning date is invalid")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("Toss KRX VI warning date range is invalid")

    @property
    def is_krx_vi_warning(self) -> bool:
        return self.exchange == "KRX" and self.warning_type in _VI_WARNING_TYPES


@dataclass(frozen=True, slots=True)
class TossKrxViEvidence:
    """Immutable current Toss warning evidence, not a strategy decision."""

    warnings: tuple[TossKrxViWarning, ...]

    def __post_init__(self) -> None:
        values = cast(object, self.warnings)
        if not isinstance(values, tuple) or not all(
            type(warning) is TossKrxViWarning
            for warning in cast(tuple[object, ...], values)
        ):
            raise ValueError("Toss KRX VI warnings must be an immutable tuple")

    @property
    def has_active_krx_vi(self) -> bool:
        return any(warning.is_krx_vi_warning for warning in self.warnings)


class TossDomesticViReadOnlyAdapter:
    """Reads Toss KRX VI evidence without account scope or write capability."""

    def __init__(self, *, transport: AsyncHttpTransport) -> None:
        self._transport = transport

    async def read_krx_vi_evidence(
        self, *, symbol: object, access_token: object
    ) -> TossKrxViEvidence:
        transport = self._transport
        try:
            outcome = await _read_evidence(
                transport=transport, symbol=symbol, access_token=access_token
            )
        finally:
            del self, transport, symbol, access_token
        validation_error, evidence = outcome
        del outcome
        if validation_error is not None:
            del evidence
            raise ValueError(validation_error)
        if evidence is None:
            raise TossIncompleteKrxViSnapshot(_INCOMPLETE)
        return evidence

    async def submit(self, *, command: object) -> NoReturn:
        del self, command
        raise BrokerWriteDisabled("Toss domestic VI write adapter is not enabled")

    async def cancel(self, *, command: object) -> NoReturn:
        del self, command
        raise BrokerWriteDisabled("Toss domestic VI write adapter is not enabled")

    async def replace(self, *, command: object) -> NoReturn:
        del self, command
        raise BrokerWriteDisabled("Toss domestic VI write adapter is not enabled")


async def _read_evidence(
    *, transport: AsyncHttpTransport, symbol: object, access_token: object
) -> tuple[str | None, TossKrxViEvidence | None]:
    request: BrokerRequest | None = None
    response: BrokerResponse | None = None
    try:
        validation_error = _validation_error(symbol=symbol, access_token=access_token)
        if validation_error is not None:
            return validation_error, None
        request = BrokerRequest(
            method="GET",
            path=f"/api/v1/stocks/{symbol}/warnings",
            headers=(("Authorization", f"Bearer {access_token}"),),
        )
        response = await transport.request(request)
        evidence = _decode_evidence(response)
        return None, evidence
    except Exception:
        return None, None
    finally:
        del transport, symbol, access_token, request, response


def _validation_error(*, symbol: object, access_token: object) -> str | None:
    if (
        not isinstance(symbol, str)
        or len(symbol) != 6
        or not symbol.isascii()
        or not symbol.isdigit()
    ):
        return _INVALID_SYMBOL
    if not _single_line_text(access_token):
        return _INVALID_TOKEN
    return None


def _decode_evidence(response: BrokerResponse) -> TossKrxViEvidence | None:
    status = response.status
    body = response.body
    del response
    try:
        if status != 200:
            return None
        payload: object = json.loads(body)
        if not isinstance(payload, Mapping):
            return None
        result = cast(Mapping[str, object], payload).get("result")
        if not isinstance(result, list):
            return None
        records = cast(list[object], result)
        return TossKrxViEvidence(
            warnings=tuple(_warning_from_record(record) for record in records)
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        return None
    finally:
        del body


def _warning_from_record(value: object) -> TossKrxViWarning:
    if not isinstance(value, Mapping):
        raise ValueError
    record = cast(Mapping[str, object], value)
    if "warningType" not in record:
        raise ValueError
    warning_type = record.get("warningType")
    exchange = record.get("exchange")
    start_value = record.get("startDate")
    start_date = None if start_value is None else _calendar_date(start_value)
    end_value = record.get("endDate")
    end_date = None if end_value is None else _calendar_date(end_value)
    return TossKrxViWarning(
        warning_type=cast(str, warning_type),
        exchange=cast(str | None, exchange),
        start_date=start_date,
        end_date=end_date,
    )


def _calendar_date(value: object) -> date:
    if (
        not isinstance(value, str)
        or len(value) != 10
        or value[4] != "-"
        or value[7] != "-"
        or not (value[:4] + value[5:7] + value[8:]).isdigit()
    ):
        raise ValueError
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError
    return parsed


def _single_line_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "\n" not in value
        and "\r" not in value
    )
