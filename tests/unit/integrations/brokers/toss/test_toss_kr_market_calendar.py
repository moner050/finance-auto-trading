from __future__ import annotations

import ast
import json
import reprlib
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import FrameType, FunctionType, TracebackType
from typing import cast

import pytest

from autotrader.integrations.brokers.common import (
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)
from autotrader.integrations.brokers.toss.kr_market_calendar import (
    TossIncompleteKrMarketCalendar,
    TossKrMarketCalendar,
    TossKrMarketCalendarDay,
    TossKrMarketCalendarReadOnlyAdapter,
    is_toss_kr_single_price_auction,
)

Payload = dict[str, object]


@dataclass
class ScriptedTransport:
    outcomes: list[BrokerResponse | BaseException]
    requests: list[BrokerRequest] = field(default_factory=list[BrokerRequest])

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _session_day(value: date, *, available: bool = True) -> dict[str, object]:
    if not available:
        return {"date": value.isoformat(), "integrated": None}
    prefix = value.isoformat()
    return {
        "date": prefix,
        "integrated": {
            "preMarket": {
                "startTime": f"{prefix}T08:00:00+09:00",
                "singlePriceAuctionStartTime": f"{prefix}T08:50:00+09:00",
                "endTime": f"{prefix}T09:00:00+09:00",
            },
            "regularMarket": {
                "startTime": f"{prefix}T09:00:00+09:00",
                "singlePriceAuctionStartTime": f"{prefix}T15:20:00+09:00",
                "endTime": f"{prefix}T15:30:00+09:00",
            },
            "afterMarket": {
                "startTime": f"{prefix}T15:30:00+09:00",
                "singlePriceAuctionEndTime": f"{prefix}T15:40:00+09:00",
                "endTime": f"{prefix}T20:00:00+09:00",
            },
        },
    }


def _calendar_payload(*, today_available: bool = True) -> bytes:
    return json.dumps(
        {
            "result": {
                "previousBusinessDay": _session_day(date(2026, 8, 11)),
                "today": _session_day(date(2026, 8, 12), available=today_available),
                "nextBusinessDay": _session_day(date(2026, 8, 13)),
            }
        }
    ).encode()


def _response(body: bytes | None = None, *, status: int = 200) -> BrokerResponse:
    return BrokerResponse(
        status=status, body=_calendar_payload() if body is None else body
    )


@pytest.mark.asyncio
async def test_reader_sends_only_bearer_get_with_optional_date_query() -> None:
    transport = ScriptedTransport([_response()])
    adapter = TossKrMarketCalendarReadOnlyAdapter(transport=transport)

    await adapter.read_kr_market_calendar(
        access_token="calendar-token", calendar_date=date(2026, 8, 12)
    )

    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path="/api/v1/market-calendar/KR?date=2026-08-12",
            headers=(("Authorization", "Bearer calendar-token"),),
        )
    ]


@pytest.mark.asyncio
async def test_reader_decodes_all_documented_auction_window_orientations_to_utc() -> (
    None
):
    adapter = TossKrMarketCalendarReadOnlyAdapter(
        transport=ScriptedTransport([_response()])
    )

    calendar = await adapter.read_kr_market_calendar(access_token="calendar-token")

    assert calendar.today.calendar_date == date(2026, 8, 12)
    assert calendar.today.windows == (
        cast(object, calendar.today.windows[0]),
        cast(object, calendar.today.windows[1]),
        cast(object, calendar.today.windows[2]),
    )
    assert tuple(
        (window.start_at, window.end_at) for window in calendar.today.windows
    ) == (
        (
            datetime(2026, 8, 11, 23, 50, tzinfo=UTC),
            datetime(2026, 8, 12, 0, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 8, 12, 6, 20, tzinfo=UTC),
            datetime(2026, 8, 12, 6, 30, tzinfo=UTC),
        ),
        (
            datetime(2026, 8, 12, 6, 30, tzinfo=UTC),
            datetime(2026, 8, 12, 6, 40, tzinfo=UTC),
        ),
    )
    assert all(type(window.start_at) is datetime for window in calendar.today.windows)


@pytest.mark.asyncio
async def test_membership_is_utc_only_and_half_open_for_every_documented_window() -> (
    None
):
    adapter = TossKrMarketCalendarReadOnlyAdapter(
        transport=ScriptedTransport([_response()])
    )
    calendar = await adapter.read_kr_market_calendar(access_token="calendar-token")

    for window in calendar.today.windows:
        assert is_toss_kr_single_price_auction(
            calendar=calendar, observed_at=window.start_at
        )
    assert not is_toss_kr_single_price_auction(
        calendar=calendar, observed_at=calendar.today.windows[0].end_at
    )
    assert is_toss_kr_single_price_auction(
        calendar=calendar, observed_at=calendar.today.windows[1].end_at
    )
    assert not is_toss_kr_single_price_auction(
        calendar=calendar, observed_at=calendar.today.windows[2].end_at
    )
    assert not is_toss_kr_single_price_auction(
        calendar=calendar, observed_at=datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    )
    for invalid in (
        datetime(2026, 8, 12, 6, 20),
        datetime(2026, 8, 12, 15, 20, tzinfo=timezone(timedelta(hours=9))),
        datetime(2026, 8, 12, 6, 20, tzinfo=timezone(timedelta(0), "not-utc")),
    ):
        with pytest.raises(ValueError):
            is_toss_kr_single_price_auction(calendar=calendar, observed_at=invalid)


@pytest.mark.asyncio
async def test_nullable_integrated_and_session_values_are_valid_no_window_data() -> (
    None
):
    payload = cast(Payload, json.loads(_calendar_payload(today_available=False)))
    result = cast(dict[str, object], payload["result"])
    today = cast(dict[str, object], result["today"])
    today["integrated"] = {
        "preMarket": None,
        "regularMarket": None,
        "afterMarket": None,
    }
    adapter = TossKrMarketCalendarReadOnlyAdapter(
        transport=ScriptedTransport([_response(json.dumps(payload).encode())])
    )

    calendar = await adapter.read_kr_market_calendar(access_token="calendar-token")

    assert calendar.today.windows == ()


@pytest.mark.asyncio
async def test_missing_integrated_property_is_valid_no_window_data() -> None:
    payload = cast(Payload, json.loads(_calendar_payload()))
    _today(payload).pop("integrated")
    adapter = TossKrMarketCalendarReadOnlyAdapter(
        transport=ScriptedTransport([_response(json.dumps(payload).encode())])
    )

    calendar = await adapter.read_kr_market_calendar(access_token="calendar-token")

    assert calendar.today.windows == ()


@pytest.mark.asyncio
async def test_missing_session_property_is_valid_no_window_data() -> None:
    payload = cast(Payload, json.loads(_calendar_payload()))
    _sessions(payload).pop("preMarket")
    adapter = TossKrMarketCalendarReadOnlyAdapter(
        transport=ScriptedTransport([_response(json.dumps(payload).encode())])
    )

    calendar = await adapter.read_kr_market_calendar(access_token="calendar-token")

    assert len(calendar.today.windows) == 2


@pytest.mark.asyncio
async def test_missing_auction_property_is_valid_no_window_data() -> None:
    payload = cast(Payload, json.loads(_calendar_payload()))
    _session(payload, "regularMarket").pop("singlePriceAuctionStartTime")
    adapter = TossKrMarketCalendarReadOnlyAdapter(
        transport=ScriptedTransport([_response(json.dumps(payload).encode())])
    )

    calendar = await adapter.read_kr_market_calendar(access_token="calendar-token")

    assert len(calendar.today.windows) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("startTime", "endTime"))
async def test_present_session_requires_non_null_start_and_end(field: str) -> None:
    payload = cast(Payload, json.loads(_calendar_payload()))
    _session(payload, "preMarket")[field] = None
    transport = ScriptedTransport([_response(json.dumps(payload).encode())])
    adapter = TossKrMarketCalendarReadOnlyAdapter(transport=transport)

    with pytest.raises(
        TossIncompleteKrMarketCalendar,
        match=r"^Toss KR market calendar is incomplete$",
    ):
        await adapter.read_kr_market_calendar(access_token="calendar-token")

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_nullable_present_auction_boundary_omits_only_its_window() -> None:
    payload = cast(Payload, json.loads(_calendar_payload()))
    _session(payload, "regularMarket")["singlePriceAuctionStartTime"] = None
    adapter = TossKrMarketCalendarReadOnlyAdapter(
        transport=ScriptedTransport([_response(json.dumps(payload).encode())])
    )

    calendar = await adapter.read_kr_market_calendar(access_token="calendar-token")

    assert len(calendar.today.windows) == 2
    assert calendar.today.windows[0].start_at == datetime(
        2026, 8, 11, 23, 50, tzinfo=UTC
    )
    assert calendar.today.windows[1].end_at == datetime(2026, 8, 12, 6, 40, tzinfo=UTC)


def _null_auction_with_non_kst_start(payload: Payload) -> None:
    _session(payload, "regularMarket").update(
        {
            "singlePriceAuctionStartTime": None,
            "startTime": "2026-08-12T09:00:00+00:00",
        }
    )


def _null_auction_with_wrong_date_end(payload: Payload) -> None:
    _session(payload, "regularMarket").update(
        {
            "singlePriceAuctionStartTime": None,
            "endTime": "2026-08-13T15:30:00+09:00",
        }
    )


def _null_auction_with_reversed_other_times(payload: Payload) -> None:
    _session(payload, "afterMarket").update(
        {
            "singlePriceAuctionEndTime": None,
            "startTime": "2026-08-12T20:00:00+09:00",
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    (
        _null_auction_with_non_kst_start,
        _null_auction_with_wrong_date_end,
        _null_auction_with_reversed_other_times,
    ),
)
async def test_nullable_auction_boundary_still_validates_other_present_times(
    mutate: Callable[[Payload], None],
) -> None:
    payload = cast(Payload, json.loads(_calendar_payload()))
    mutate(payload)
    transport = ScriptedTransport([_response(json.dumps(payload).encode())])
    adapter = TossKrMarketCalendarReadOnlyAdapter(transport=transport)

    with pytest.raises(
        TossIncompleteKrMarketCalendar,
        match=r"^Toss KR market calendar is incomplete$",
    ):
        await adapter.read_kr_market_calendar(access_token="calendar-token")

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_out_of_order_calendar_days_fail_closed_after_valid_day_decoding() -> (
    None
):
    payload = cast(Payload, json.loads(_calendar_payload()))
    result = _result(payload)
    result["previousBusinessDay"], result["today"] = (
        result["today"],
        result["previousBusinessDay"],
    )
    transport = ScriptedTransport([_response(json.dumps(payload).encode())])
    adapter = TossKrMarketCalendarReadOnlyAdapter(transport=transport)

    with pytest.raises(
        TossIncompleteKrMarketCalendar,
        match=r"^Toss KR market calendar is incomplete$",
    ):
        await adapter.read_kr_market_calendar(access_token="calendar-token")

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_requested_date_must_match_provider_today_day() -> None:
    transport = ScriptedTransport([_response()])
    adapter = TossKrMarketCalendarReadOnlyAdapter(transport=transport)

    with pytest.raises(
        TossIncompleteKrMarketCalendar,
        match=r"^Toss KR market calendar is incomplete$",
    ):
        await adapter.read_kr_market_calendar(
            access_token="calendar-token", calendar_date=date(2026, 8, 11)
        )

    assert len(transport.requests) == 1


def test_calendar_value_object_rejects_nonascending_days_before_helper_can_run() -> (
    None
):
    previous = TossKrMarketCalendarDay(calendar_date=date(2026, 8, 12), windows=())
    today = TossKrMarketCalendarDay(calendar_date=date(2026, 8, 11), windows=())
    next_day = TossKrMarketCalendarDay(calendar_date=date(2026, 8, 13), windows=())

    with pytest.raises(ValueError):
        TossKrMarketCalendar(
            previous_business_day=previous,
            today=today,
            next_business_day=next_day,
        )


def _result(payload: Payload) -> Payload:
    return cast(Payload, payload["result"])


def _today(payload: Payload) -> Payload:
    return cast(Payload, _result(payload)["today"])


def _sessions(payload: Payload) -> Payload:
    return cast(Payload, _today(payload)["integrated"])


def _session(payload: Payload, name: str) -> Payload:
    return cast(Payload, _sessions(payload)[name])


def _remove_result(payload: Payload) -> None:
    payload.pop("result")


def _remove_today(payload: Payload) -> None:
    _result(payload).pop("today")


def _remove_day_date(payload: Payload) -> None:
    _today(payload).pop("date")


def _missing_present_session_field(payload: Payload) -> None:
    _today(payload)["integrated"] = {"preMarket": {}}


def _naive_session_time(payload: Payload) -> None:
    _session(payload, "preMarket")["startTime"] = "2026-08-12T08:00:00"


def _non_kst_session_time(payload: Payload) -> None:
    _session(payload, "preMarket")["startTime"] = "2026-08-12T08:00:00+00:00"


def _mismatched_session_date(payload: Payload) -> None:
    _session(payload, "preMarket")["startTime"] = "2026-08-13T08:00:00+09:00"


def _reversed_regular_auction(payload: Payload) -> None:
    _session(payload, "regularMarket")["singlePriceAuctionStartTime"] = (
        "2026-08-12T09:00:00+09:00"
    )


def _reversed_after_auction(payload: Payload) -> None:
    _session(payload, "afterMarket")["singlePriceAuctionEndTime"] = (
        "2026-08-12T15:30:00+09:00"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    (
        _remove_result,
        _remove_today,
        _remove_day_date,
        _missing_present_session_field,
        _naive_session_time,
        _non_kst_session_time,
        _mismatched_session_date,
        _reversed_regular_auction,
        _reversed_after_auction,
    ),
)
async def test_malformed_present_calendar_data_fails_closed(
    mutate: Callable[[Payload], None],
) -> None:
    payload = cast(Payload, json.loads(_calendar_payload()))
    mutate(payload)
    adapter = TossKrMarketCalendarReadOnlyAdapter(
        transport=ScriptedTransport([_response(json.dumps(payload).encode())])
    )

    with pytest.raises(
        TossIncompleteKrMarketCalendar,
        match=r"^Toss KR market calendar is incomplete$",
    ):
        await adapter.read_kr_market_calendar(access_token="calendar-token")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    (
        _response(b"not-json"),
        _response(status=503),
        RuntimeError("provider raw failure"),
    ),
)
async def test_provider_and_transport_failures_are_incomplete(outcome: object) -> None:
    adapter = TossKrMarketCalendarReadOnlyAdapter(
        transport=ScriptedTransport([cast(BrokerResponse | BaseException, outcome)])
    )

    with pytest.raises(
        TossIncompleteKrMarketCalendar,
        match=r"^Toss KR market calendar is incomplete$",
    ):
        await adapter.read_kr_market_calendar(access_token="calendar-token")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("access_token", "calendar_date"),
    (("", None), ("broken\n-token", None), ("calendar-token", "2026-08-12")),
)
async def test_invalid_inputs_fail_before_transport(
    access_token: object, calendar_date: object
) -> None:
    transport = ScriptedTransport([_response()])
    adapter = TossKrMarketCalendarReadOnlyAdapter(transport=transport)

    with pytest.raises(ValueError):
        await adapter.read_kr_market_calendar(
            access_token=access_token, calendar_date=calendar_date
        )

    assert transport.requests == []


@pytest.mark.asyncio
async def test_write_methods_are_blocked_before_transport() -> None:
    transport = ScriptedTransport([_response()])
    adapter = TossKrMarketCalendarReadOnlyAdapter(transport=transport)

    for method in (adapter.submit, adapter.cancel, adapter.replace):
        with pytest.raises(
            BrokerWriteDisabled,
            match=r"^Toss KR market calendar write adapter is not enabled$",
        ):
            await method(command=object())

    assert transport.requests == []


@dataclass(frozen=True)
class _PrivacyCapture:
    forbidden: tuple[object, ...]
    private_contents: tuple[str, ...]
    request_count: int


_privacy_capture: _PrivacyCapture | None = None


async def _provider_privacy_probe() -> BaseException:
    global _privacy_capture
    token = "calendar-token-provider-private-812"
    requested_date = date(2026, 8, 12)
    raw = b"calendar-raw-private-812"
    response = _response(raw)
    transport = ScriptedTransport([response])
    adapter = TossKrMarketCalendarReadOnlyAdapter(transport=transport)
    public_error: BaseException | None = None
    request: BrokerRequest | None = None
    try:
        with pytest.raises(TossIncompleteKrMarketCalendar) as raised:
            await adapter.read_kr_market_calendar(
                access_token=token, calendar_date=requested_date
            )
        public_error = raised.value
        request = transport.requests[0]
        _privacy_capture = _PrivacyCapture(
            forbidden=(
                token,
                requested_date,
                raw,
                request,
                response,
                transport,
                adapter,
            ),
            private_contents=(token, requested_date.isoformat(), raw.decode()),
            request_count=len(transport.requests),
        )
        return public_error
    finally:
        del (
            token,
            requested_date,
            raw,
            response,
            transport,
            adapter,
            public_error,
            request,
        )


async def _invalid_privacy_probe() -> BaseException:
    global _privacy_capture
    token = "calendar-token-invalid-private-812"
    invalid_date = "calendar-date-invalid-private-812"
    transport = ScriptedTransport([_response()])
    adapter = TossKrMarketCalendarReadOnlyAdapter(transport=transport)
    public_error: BaseException | None = None
    try:
        with pytest.raises(ValueError) as raised:
            await adapter.read_kr_market_calendar(
                access_token=token, calendar_date=invalid_date
            )
        public_error = raised.value
        _privacy_capture = _PrivacyCapture(
            forbidden=(token, invalid_date, transport, adapter),
            private_contents=(token, invalid_date),
            request_count=len(transport.requests),
        )
        return public_error
    finally:
        del token, invalid_date, transport, adapter, public_error


async def _transport_privacy_probe() -> BaseException:
    global _privacy_capture
    token = "calendar-token-transport-private-812"
    requested_date = date(2026, 8, 12)
    raw = b"calendar-transport-private-812"
    transport_error = RuntimeError(raw.decode())
    transport = ScriptedTransport([transport_error])
    adapter = TossKrMarketCalendarReadOnlyAdapter(transport=transport)
    public_error: BaseException | None = None
    request: BrokerRequest | None = None
    try:
        with pytest.raises(TossIncompleteKrMarketCalendar) as raised:
            await adapter.read_kr_market_calendar(
                access_token=token, calendar_date=requested_date
            )
        public_error = raised.value
        request = transport.requests[0]
        _privacy_capture = _PrivacyCapture(
            forbidden=(
                token,
                requested_date,
                raw,
                request,
                transport_error,
                transport,
                adapter,
            ),
            private_contents=(token, requested_date.isoformat(), raw.decode()),
            request_count=len(transport.requests),
        )
        return public_error
    finally:
        del (
            token,
            requested_date,
            raw,
            transport_error,
            transport,
            adapter,
            public_error,
            request,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("factory", "expected_exception", "expected_request_count"),
    (
        (_provider_privacy_probe, TossIncompleteKrMarketCalendar, 1),
        (_transport_privacy_probe, TossIncompleteKrMarketCalendar, 1),
        (_invalid_privacy_probe, ValueError, 0),
    ),
)
async def test_public_errors_do_not_retain_private_values(
    factory: Callable[[], Awaitable[BaseException]],
    expected_exception: type[BaseException],
    expected_request_count: int,
) -> None:
    raised = await factory()
    capture = _privacy_capture
    assert capture is not None

    assert isinstance(raised, expected_exception)
    assert capture.request_count == expected_request_count
    assert raised.__cause__ is None
    assert raised.__context__ is None
    reachable = tuple(_error_reachable_values(raised))
    assert any(isinstance(value, FrameType) for value in reachable)
    assert any(
        isinstance(value, FrameType)
        and value.f_code.co_filename.endswith("kr_market_calendar.py")
        for value in reachable
    )
    assert all(
        all(value is not forbidden for value in reachable)
        for forbidden in capture.forbidden
    )
    assert all(
        not _contains_private_content(value, capture.private_contents)
        for value in reachable
    )


def test_module_has_only_shared_contract_import_and_clean_fresh_import() -> None:
    module_path = Path("src/autotrader/integrations/brokers/toss/kr_market_calendar.py")
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    forbidden = (
        "adapter",
        "execution",
        "apps",
        "operations",
        "persistence",
        "runtime",
        "config",
        "risk",
        "observability",
        "contracts",
    )
    assert all(
        not isinstance(node, ast.Import)
        or all(not alias.name.startswith("autotrader.") for alias in node.names)
        for node in ast.walk(tree)
    )
    assert all(
        not isinstance(node, ast.ImportFrom)
        or node.module is None
        or not node.module.startswith("autotrader.")
        or node.module == "autotrader.integrations.brokers.common"
        for node in ast.walk(tree)
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; "
            "import autotrader.integrations.brokers.toss.kr_market_calendar; "
            "print('\\n'.join(sorted(sys.modules)))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = set(completed.stdout.splitlines())
    assert not any(
        module.startswith("autotrader.") and any(part in module for part in forbidden)
        for module in loaded
    )


def _error_reachable_values(error: BaseException) -> Iterator[object]:
    pending: list[object] = [error]
    visited: set[int] = set()
    while pending and len(visited) < 750:
        value = pending.pop()
        if id(value) in visited:
            continue
        visited.add(id(value))
        yield value
        if isinstance(value, BaseException):
            pending.extend(value.args)
            if value.__cause__ is not None:
                pending.append(value.__cause__)
            if value.__context__ is not None:
                pending.append(value.__context__)
            pending.append(value.__traceback__)
        elif isinstance(value, TracebackType):
            pending.extend((value.tb_frame, value.tb_next))
        elif isinstance(value, FrameType):
            pending.extend(value.f_locals.values())
            caller = value.f_back
            for _ in range(6):
                if caller is None:
                    break
                pending.append(caller)
                caller = caller.f_back
        elif isinstance(value, (tuple, list, set, frozenset)):
            pending.extend(cast(tuple[object, ...], value))
        elif isinstance(value, dict):
            pending.extend(cast(dict[object, object], value).items())
        elif isinstance(value, FunctionType):
            if value.__closure__ is not None:
                pending.extend(cell.cell_contents for cell in value.__closure__)
            pending.extend(value.__defaults__ or ())
            if value.__kwdefaults__ is not None:
                pending.extend(value.__kwdefaults__.values())
        elif hasattr(value, "__dict__"):
            pending.extend(cast(dict[str, object], value.__dict__).values())
        else:
            for owner in type(value).__mro__:
                slots = owner.__dict__.get("__slots__", ())
                if isinstance(slots, str):
                    slots = (slots,)
                for slot in cast(tuple[str, ...], slots):
                    if hasattr(value, slot):
                        pending.append(getattr(value, slot))


def _contains_private_content(value: object, contents: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(content in value for content in contents)
    if isinstance(value, bytes):
        return any(content.encode() in value for content in contents)
    try:
        rendered = _bounded_repr(value)
    except Exception:
        return False
    return any(content in rendered for content in contents)


def _bounded_repr(value: object) -> str:
    renderer = reprlib.Repr()
    renderer.maxother = 1_024
    renderer.maxstring = 1_024
    return renderer.repr(value)
