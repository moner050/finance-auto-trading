from __future__ import annotations

import ast
import asyncio
import json
import reprlib
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import FrameType, FunctionType, MethodType, TracebackType
from typing import cast
from uuid import UUID

import pytest

from autotrader.domain.toss_hlit_market_safety import (
    TossHlitKrxMarketSafetyEvidence,
    TossHlitKrxMarketSafetySourceEvidence,
)
from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerRequest,
    BrokerResponse,
    BrokerWriteDisabled,
)
from autotrader.integrations.brokers.toss.domestic_vi import (
    TossDomesticViReadOnlyAdapter,
    TossKrxViEvidence,
    TossKrxViWarning,
)
from autotrader.integrations.brokers.toss.hlit_market_safety import (
    TossHlitKrxMarketSafetyReadOnlyObserver,
    TossIncompleteHlitKrxMarketSafetySnapshot,
    build_toss_hlit_krx_market_safety_evidence,
    build_toss_hlit_krx_market_safety_source_evidence,
)
from autotrader.integrations.brokers.toss.kr_market_calendar import (
    TossKrMarketCalendar,
    TossKrMarketCalendarDay,
    TossKrMarketCalendarReadOnlyAdapter,
    TossKrSinglePriceAuctionWindow,
)


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


def _vi_response(result: bytes = b"[]", *, status: int = 200) -> BrokerResponse:
    return BrokerResponse(status=status, body=b'{"result":' + result + b"}")


def _calendar_response(
    *, status: int = 200, body: bytes | None = None
) -> BrokerResponse:
    payload = {
        "result": {
            "previousBusinessDay": _calendar_day("2026-08-11"),
            "today": _calendar_day("2026-08-12"),
            "nextBusinessDay": _calendar_day("2026-08-13"),
        }
    }
    return BrokerResponse(
        status=status, body=json.dumps(payload).encode() if body is None else body
    )


def _calendar_day(calendar_date: str) -> dict[str, object]:
    return {
        "date": calendar_date,
        "integrated": {
            "regularMarket": {
                "startTime": f"{calendar_date}T09:00:00+09:00",
                "singlePriceAuctionStartTime": f"{calendar_date}T15:20:00+09:00",
                "endTime": f"{calendar_date}T15:30:00+09:00",
            }
        },
    }


def _calendar() -> TossKrMarketCalendar:
    return TossKrMarketCalendar(
        previous_business_day=TossKrMarketCalendarDay(
            calendar_date=date(2026, 8, 11), windows=()
        ),
        today=TossKrMarketCalendarDay(
            calendar_date=date(2026, 8, 12),
            windows=(
                TossKrSinglePriceAuctionWindow(
                    start_at=datetime(2026, 8, 12, 6, 20, tzinfo=UTC),
                    end_at=datetime(2026, 8, 12, 6, 30, tzinfo=UTC),
                ),
            ),
        ),
        next_business_day=TossKrMarketCalendarDay(
            calendar_date=date(2026, 8, 13), windows=()
        ),
    )


def _active_vi_evidence() -> TossKrxViEvidence:
    return TossKrxViEvidence(
        warnings=(
            TossKrxViWarning(
                warning_type="VI_STATIC",
                exchange="KRX",
                start_date=date(2026, 8, 12),
                end_date=None,
            ),
        )
    )


def _two_warning_vi_evidence() -> TossKrxViEvidence:
    return TossKrxViEvidence(
        warnings=(
            *_active_vi_evidence().warnings,
            TossKrxViWarning(
                warning_type="UNKNOWN_PROVIDER_WARNING",
                exchange=None,
                start_date=None,
                end_date=None,
            ),
        )
    )


def _source_evidence() -> TossHlitKrxMarketSafetySourceEvidence:
    observed_at = datetime(2026, 8, 12, 6, 20, tzinfo=UTC)
    return build_toss_hlit_krx_market_safety_source_evidence(
        symbol="005930",
        observed_at=observed_at,
        vi_evidence=_two_warning_vi_evidence(),
        calendar=_calendar(),
        vi_source_id=UUID("018f27e6-3b4c-7a10-8123-123456789abc"),
        vi_expires_at=observed_at + timedelta(minutes=1),
        calendar_source_id=UUID("018f27e6-3b4c-7a10-8123-123456789abd"),
        calendar_expires_at=observed_at + timedelta(minutes=2),
    )


def test_builds_canonical_toss_vi_and_calendar_source_provenance() -> None:
    source = _source_evidence()

    assert source.evidence == TossHlitKrxMarketSafetyEvidence(
        symbol="005930",
        observed_at=datetime(2026, 8, 12, 6, 20, tzinfo=UTC),
        has_active_krx_vi=True,
        is_single_price_auction=True,
    )
    assert len(source.vi_source_hash) == 32
    assert len(source.calendar_source_hash) == 32
    assert len(source.source_hash) == 32


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("vi_source_id", UUID(int=3)),
        ("vi_source_hash", b"x" * 32),
        ("vi_expires_at", datetime(2026, 8, 12, 6, 22, tzinfo=UTC)),
        ("calendar_source_id", UUID(int=4)),
        ("calendar_source_hash", b"y" * 32),
        ("calendar_expires_at", datetime(2026, 8, 12, 6, 23, tzinfo=UTC)),
        (
            "evidence",
            TossHlitKrxMarketSafetyEvidence(
                symbol="005930",
                observed_at=datetime(2026, 8, 12, 6, 20, tzinfo=UTC),
                has_active_krx_vi=False,
                is_single_price_auction=True,
            ),
        ),
    ),
)
def test_source_parent_rejects_stale_hash_after_bound_field_mutation(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        replace(_source_evidence(), **{field: value})


def test_raw_provider_order_and_calendar_window_bounds_change_component_hashes() -> (
    None
):
    baseline = _source_evidence()
    observed_at = baseline.evidence.observed_at
    reversed_warnings = TossKrxViEvidence(
        warnings=tuple(reversed(_two_warning_vi_evidence().warnings))
    )
    changed_vi = build_toss_hlit_krx_market_safety_source_evidence(
        symbol="005930",
        observed_at=observed_at,
        vi_evidence=reversed_warnings,
        calendar=_calendar(),
        vi_source_id=baseline.vi_source_id,
        vi_expires_at=baseline.vi_expires_at,
        calendar_source_id=baseline.calendar_source_id,
        calendar_expires_at=baseline.calendar_expires_at,
    )
    calendar = _calendar()
    changed_calendar = replace(
        calendar,
        today=replace(
            calendar.today,
            windows=(
                TossKrSinglePriceAuctionWindow(
                    start_at=datetime(2026, 8, 12, 6, 19, tzinfo=UTC),
                    end_at=datetime(2026, 8, 12, 6, 30, tzinfo=UTC),
                ),
            ),
        ),
    )
    changed_window = build_toss_hlit_krx_market_safety_source_evidence(
        symbol="005930",
        observed_at=observed_at,
        vi_evidence=_two_warning_vi_evidence(),
        calendar=changed_calendar,
        vi_source_id=baseline.vi_source_id,
        vi_expires_at=baseline.vi_expires_at,
        calendar_source_id=baseline.calendar_source_id,
        calendar_expires_at=baseline.calendar_expires_at,
    )

    assert changed_vi.vi_source_hash != baseline.vi_source_hash
    assert changed_window.calendar_source_hash != baseline.calendar_source_hash
    assert changed_vi.source_hash != baseline.source_hash
    assert changed_window.source_hash != baseline.source_hash


@pytest.mark.parametrize(
    ("vi_expires_at", "calendar_expires_at"),
    (
        (
            datetime(2026, 8, 12, 6, 20, tzinfo=UTC),
            datetime(2026, 8, 12, 6, 22, tzinfo=UTC),
        ),
        (
            datetime(2026, 8, 12, 6, 21, 0, 1, tzinfo=UTC),
            datetime(2026, 8, 12, 6, 22, tzinfo=UTC),
        ),
        (
            datetime(2026, 8, 12, 6, 21, tzinfo=UTC),
            datetime(2026, 8, 12, 6, 20, tzinfo=UTC),
        ),
    ),
)
def test_source_builder_requires_independent_exclusive_whole_second_expiries(
    vi_expires_at: datetime, calendar_expires_at: datetime
) -> None:
    observed_at = datetime(2026, 8, 12, 6, 20, tzinfo=UTC)
    with pytest.raises(ValueError):
        build_toss_hlit_krx_market_safety_source_evidence(
            symbol="005930",
            observed_at=observed_at,
            vi_evidence=_active_vi_evidence(),
            calendar=_calendar(),
            vi_source_id=UUID(int=1),
            vi_expires_at=vi_expires_at,
            calendar_source_id=UUID(int=2),
            calendar_expires_at=calendar_expires_at,
        )


def test_composes_active_documented_krx_vi_at_auction_start_as_frozen_scalars() -> None:
    observed_at = datetime(2026, 8, 12, 6, 20, tzinfo=UTC)

    evidence = build_toss_hlit_krx_market_safety_evidence(
        symbol="005930",
        observed_at=observed_at,
        vi_evidence=_active_vi_evidence(),
        calendar=_calendar(),
    )

    assert evidence == TossHlitKrxMarketSafetyEvidence(
        symbol="005930",
        observed_at=observed_at,
        has_active_krx_vi=True,
        is_single_price_auction=True,
    )
    assert not hasattr(evidence, "__dict__")
    with pytest.raises(FrozenInstanceError):
        evidence.symbol = "000660"  # type: ignore[misc]


@pytest.mark.parametrize(
    "vi_evidence",
    (
        TossKrxViEvidence(warnings=()),
        TossKrxViEvidence(
            warnings=(
                TossKrxViWarning(
                    warning_type="UNKNOWN_PROVIDER_WARNING",
                    exchange="KRX",
                    start_date=None,
                    end_date=None,
                ),
            )
        ),
    ),
)
def test_composes_inactive_or_unknown_vi_as_false(
    vi_evidence: TossKrxViEvidence,
) -> None:
    evidence = build_toss_hlit_krx_market_safety_evidence(
        symbol="005930",
        observed_at=datetime(2026, 8, 12, 6, 19, tzinfo=UTC),
        vi_evidence=vi_evidence,
        calendar=_calendar(),
    )

    assert evidence.has_active_krx_vi is False
    assert evidence.is_single_price_auction is False


def test_composes_documented_auction_as_half_open_interval() -> None:
    calendar = _calendar()

    at_start = build_toss_hlit_krx_market_safety_evidence(
        symbol="005930",
        observed_at=datetime(2026, 8, 12, 6, 20, tzinfo=UTC),
        vi_evidence=_active_vi_evidence(),
        calendar=calendar,
    )
    at_end = build_toss_hlit_krx_market_safety_evidence(
        symbol="005930",
        observed_at=datetime(2026, 8, 12, 6, 30, tzinfo=UTC),
        vi_evidence=_active_vi_evidence(),
        calendar=calendar,
    )

    assert at_start.is_single_price_auction is True
    assert at_end.is_single_price_auction is False


class _DerivedViEvidence(TossKrxViEvidence):
    pass


class _DerivedCalendar(TossKrMarketCalendar):
    pass


@pytest.mark.parametrize(
    ("symbol", "observed_at", "vi_evidence", "calendar"),
    (
        (
            "00593",
            datetime(2026, 8, 12, 6, 20, tzinfo=UTC),
            _active_vi_evidence(),
            _calendar(),
        ),
        (
            "005930",
            datetime(2026, 8, 12, 6, 20),
            _active_vi_evidence(),
            _calendar(),
        ),
        (
            "005930",
            datetime(2026, 8, 12, 15, 20, tzinfo=timezone(timedelta(hours=9))),
            _active_vi_evidence(),
            _calendar(),
        ),
        (
            "005930",
            datetime(2026, 8, 12, 6, 20, tzinfo=timezone(timedelta(0), "not-utc")),
            _active_vi_evidence(),
            _calendar(),
        ),
        (
            "005930",
            datetime(2026, 8, 12, 6, 20, tzinfo=UTC),
            _DerivedViEvidence(warnings=()),
            _calendar(),
        ),
        (
            "005930",
            datetime(2026, 8, 12, 6, 20, tzinfo=UTC),
            _active_vi_evidence(),
            _DerivedCalendar(
                _calendar().previous_business_day,
                _calendar().today,
                _calendar().next_business_day,
            ),
        ),
        (
            "005930",
            datetime(2026, 8, 13, 6, 20, tzinfo=UTC),
            _active_vi_evidence(),
            _calendar(),
        ),
    ),
)
def test_builder_rejects_invalid_or_nonexact_inputs(
    symbol: object, observed_at: object, vi_evidence: object, calendar: object
) -> None:
    with pytest.raises(ValueError):
        build_toss_hlit_krx_market_safety_evidence(
            symbol=symbol,
            observed_at=observed_at,
            vi_evidence=vi_evidence,
            calendar=calendar,
        )


@pytest.mark.parametrize(
    ("symbol", "observed_at", "has_active_krx_vi", "is_single_price_auction"),
    (
        ("00593", datetime(2026, 8, 12, 6, 20, tzinfo=UTC), True, True),
        ("005930", datetime(2026, 8, 12, 6, 20), True, True),
        (
            "005930",
            datetime(2026, 8, 12, 15, 20, tzinfo=timezone(timedelta(hours=9))),
            True,
            True,
        ),
        ("005930", datetime(2026, 8, 12, 6, 20, tzinfo=UTC), 1, True),
        ("005930", datetime(2026, 8, 12, 6, 20, tzinfo=UTC), True, 0),
    ),
)
def test_output_value_object_rejects_invalid_fields(
    symbol: object,
    observed_at: object,
    has_active_krx_vi: object,
    is_single_price_auction: object,
) -> None:
    with pytest.raises(ValueError):
        TossHlitKrxMarketSafetyEvidence(
            symbol=cast(str, symbol),
            observed_at=cast(datetime, observed_at),
            has_active_krx_vi=cast(bool, has_active_krx_vi),
            is_single_price_auction=cast(bool, is_single_price_auction),
        )


def test_module_imports_only_narrow_toss_evidence_and_loads_no_broad_boundaries() -> (
    None
):
    module_path = Path("src/autotrader/integrations/brokers/toss/hlit_market_safety.py")
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("autotrader.")
    }
    forbidden = (
        "adapter",
        "strategy",
        "h3",
        "execution",
        "application",
        "apps",
        "operations",
        "persistence",
        "runtime",
        "config",
        "risk",
        "observability",
        "contracts",
        "transport",
    )

    assert imports == {
        "autotrader.domain.toss_hlit_market_safety",
        "autotrader.integrations.brokers.common",
        "autotrader.integrations.brokers.toss.domestic_vi",
        "autotrader.integrations.brokers.toss.kr_market_calendar",
    }
    assert all(
        not isinstance(node, ast.Import)
        or all(not alias.name.startswith("autotrader.") for alias in node.names)
        for node in ast.walk(tree)
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; "
            "import autotrader.integrations.brokers.toss.hlit_market_safety; "
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("vi_result", "observed_at", "has_active_vi", "is_auction"),
    (
        (
            b'[{"warningType":"VI_STATIC","exchange":"KRX",'
            b'"startDate":"2026-08-12","endDate":null}]',
            datetime(2026, 8, 12, 6, 20, tzinfo=UTC),
            True,
            True,
        ),
        (b"[]", datetime(2026, 8, 12, 6, 30, tzinfo=UTC), False, False),
    ),
)
async def test_observer_reads_bearer_only_vi_then_kst_calendar_as_scalars(
    vi_result: bytes,
    observed_at: datetime,
    has_active_vi: bool,
    is_auction: bool,
) -> None:
    transport = ScriptedTransport([_vi_response(vi_result), _calendar_response()])

    evidence = await TossHlitKrxMarketSafetyReadOnlyObserver(
        transport=transport
    ).read_snapshot(
        symbol="005930", observed_at=observed_at, access_token="snapshot-token"
    )

    assert evidence == TossHlitKrxMarketSafetyEvidence(
        symbol="005930",
        observed_at=observed_at,
        has_active_krx_vi=has_active_vi,
        is_single_price_auction=is_auction,
    )
    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path="/api/v1/stocks/005930/warnings",
            headers=(("Authorization", "Bearer snapshot-token"),),
        ),
        BrokerRequest(
            method="GET",
            path="/api/v1/market-calendar/KR?date=2026-08-12",
            headers=(("Authorization", "Bearer snapshot-token"),),
        ),
    ]


@pytest.mark.asyncio
async def test_observer_uses_kst_calendar_date_from_exact_utc_observation() -> None:
    transport = ScriptedTransport([_vi_response(), _calendar_response()])

    evidence = await TossHlitKrxMarketSafetyReadOnlyObserver(
        transport=transport
    ).read_snapshot(
        symbol="005930",
        observed_at=datetime(2026, 8, 11, 15, 0, tzinfo=UTC),
        access_token="snapshot-token",
    )

    assert evidence.is_single_price_auction is False
    assert transport.requests[1].path == "/api/v1/market-calendar/KR?date=2026-08-12"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbol", "observed_at", "access_token"),
    (
        ("00593x", datetime(2026, 8, 12, 6, 20, tzinfo=UTC), "snapshot-token"),
        ("005930", datetime(2026, 8, 12, 6, 20), "snapshot-token"),
        ("005930", datetime.max.replace(tzinfo=UTC), "snapshot-token"),
        ("005930", datetime(2026, 8, 12, 6, 20, tzinfo=UTC), ""),
    ),
)
async def test_observer_invalid_inputs_fail_as_incomplete_before_transport(
    symbol: object, observed_at: object, access_token: object
) -> None:
    transport = ScriptedTransport([])

    with pytest.raises(TossIncompleteHlitKrxMarketSafetySnapshot) as raised:
        await TossHlitKrxMarketSafetyReadOnlyObserver(
            transport=transport
        ).read_snapshot(
            symbol=symbol, observed_at=observed_at, access_token=access_token
        )

    assert transport.requests == []
    assert str(raised.value) == "Toss HLIT market-safety snapshot is incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcomes", "request_count"),
    (
        ([_vi_response(status=503)], 1),
        ([BrokerResponse(status=200, body=b"not-json")], 1),
        ([_vi_response(), _calendar_response(status=503)], 2),
        ([_vi_response(), BrokerResponse(status=200, body=b"not-json")], 2),
        ([RuntimeError("private VI transport error")], 1),
        ([asyncio.CancelledError("private VI cancellation")], 1),
        ([_vi_response(), RuntimeError("private calendar transport error")], 2),
    ),
)
async def test_observer_scrubs_all_provider_and_transport_failures(
    outcomes: list[BrokerResponse | BaseException], request_count: int
) -> None:
    transport = ScriptedTransport(outcomes)

    with pytest.raises(TossIncompleteHlitKrxMarketSafetySnapshot) as raised:
        await TossHlitKrxMarketSafetyReadOnlyObserver(
            transport=transport
        ).read_snapshot(
            symbol="005930",
            observed_at=datetime(2026, 8, 12, 6, 20, tzinfo=UTC),
            access_token="snapshot-token",
        )

    assert str(raised.value) == "Toss HLIT market-safety snapshot is incomplete"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert len(transport.requests) == request_count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interruption",
    (KeyboardInterrupt("private interrupt"), SystemExit("private exit")),
)
async def test_observer_propagates_process_control_interrupts(
    interruption: BaseException,
) -> None:
    transport = ScriptedTransport([interruption])

    with pytest.raises(type(interruption)) as raised:
        await TossHlitKrxMarketSafetyReadOnlyObserver(
            transport=transport
        ).read_snapshot(
            symbol="005930",
            observed_at=datetime(2026, 8, 12, 6, 20, tzinfo=UTC),
            access_token="snapshot-token",
        )

    assert raised.value is interruption
    assert len(transport.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ("submit", "cancel", "replace"))
async def test_observer_write_methods_are_disabled_before_transport(
    method: str,
) -> None:
    transport = ScriptedTransport([])
    observer = TossHlitKrxMarketSafetyReadOnlyObserver(transport=transport)

    with pytest.raises(BrokerWriteDisabled) as raised:
        await getattr(observer, method)(command=object())

    assert (
        str(raised.value)
        == "Toss HLIT market-safety snapshot write adapter is not enabled"
    )
    assert transport.requests == []


@dataclass(frozen=True, slots=True)
class _PrivacyCapture:
    forbidden: tuple[object, ...]
    private_contents: tuple[str, ...]
    request_count: int


_privacy_capture: _PrivacyCapture | None = None


async def _observer_privacy_probe() -> BaseException:
    global _privacy_capture
    import autotrader.integrations.brokers.toss.hlit_market_safety as module

    token = "snapshot-private-token-812"
    symbol = "005930"
    observed_at = datetime(2026, 8, 12, 6, 20, tzinfo=UTC)
    vi_response = _vi_response(
        b'[{"warningType":"VI_STATIC","exchange":"KRX",'
        b'"startDate":"2026-08-12","endDate":null}]'
    )
    calendar_response = _calendar_response()
    original_error = RuntimeError("snapshot-original-error-812")
    transport = ScriptedTransport([vi_response, calendar_response])
    built: dict[str, object] = {}

    class CapturingViAdapter:
        instance: object | None = None

        def __init__(self, *, transport: AsyncHttpTransport) -> None:
            self.inner = TossDomesticViReadOnlyAdapter(transport=transport)
            type(self).instance = self

        async def read_krx_vi_evidence(
            self, *, symbol: object, access_token: object
        ) -> TossKrxViEvidence:
            evidence = await self.inner.read_krx_vi_evidence(
                symbol=symbol, access_token=access_token
            )
            self.evidence = evidence
            return evidence

    class CapturingCalendarAdapter:
        instance: object | None = None

        def __init__(self, *, transport: AsyncHttpTransport) -> None:
            self.inner = TossKrMarketCalendarReadOnlyAdapter(transport=transport)
            type(self).instance = self

        async def read_kr_market_calendar(
            self, *, access_token: object, calendar_date: object = None
        ) -> TossKrMarketCalendar:
            calendar = await self.inner.read_kr_market_calendar(
                access_token=access_token, calendar_date=calendar_date
            )
            self.calendar = calendar
            return calendar

    def fail_builder(
        _built: dict[str, object] = built,
        _original_error: BaseException = original_error,
        **kwargs: object,
    ) -> TossHlitKrxMarketSafetyEvidence:
        _built.update(kwargs)
        raise _original_error

    original_vi = module.TossDomesticViReadOnlyAdapter
    original_calendar = module.TossKrMarketCalendarReadOnlyAdapter
    original_builder = module.build_toss_hlit_krx_market_safety_evidence
    vi_adapter: object | None = None
    calendar_adapter: object | None = None
    public_error: BaseException | None = None
    try:
        module.TossDomesticViReadOnlyAdapter = CapturingViAdapter
        module.TossKrMarketCalendarReadOnlyAdapter = CapturingCalendarAdapter
        module.build_toss_hlit_krx_market_safety_evidence = fail_builder
        with pytest.raises(TossIncompleteHlitKrxMarketSafetySnapshot) as raised:
            await TossHlitKrxMarketSafetyReadOnlyObserver(
                transport=transport
            ).read_snapshot(symbol=symbol, observed_at=observed_at, access_token=token)
        public_error = raised.value
        vi_instance = CapturingViAdapter.instance
        calendar_instance = CapturingCalendarAdapter.instance
        assert vi_instance is not None
        assert calendar_instance is not None
        vi_adapter = cast(CapturingViAdapter, vi_instance)
        calendar_adapter = cast(CapturingCalendarAdapter, calendar_instance)
        _privacy_capture = _PrivacyCapture(
            forbidden=(
                token,
                symbol,
                observed_at,
                vi_response,
                calendar_response,
                *transport.requests,
                transport,
                vi_adapter,
                vi_adapter.inner,
                vi_adapter.evidence,
                calendar_adapter,
                calendar_adapter.inner,
                calendar_adapter.calendar,
                original_error,
            ),
            private_contents=(
                token,
                symbol,
                observed_at.isoformat(),
                str(original_error),
            ),
            request_count=len(transport.requests),
        )
        assert public_error is not None
        del raised, vi_instance, calendar_instance
        return public_error
    finally:
        module.TossDomesticViReadOnlyAdapter = original_vi
        module.TossKrMarketCalendarReadOnlyAdapter = original_calendar
        module.build_toss_hlit_krx_market_safety_evidence = original_builder
        del (
            token,
            symbol,
            observed_at,
            vi_response,
            calendar_response,
            original_error,
            transport,
            built,
            original_vi,
            original_calendar,
            original_builder,
            public_error,
            vi_adapter,
            calendar_adapter,
            CapturingViAdapter,
            CapturingCalendarAdapter,
            fail_builder,
            module,
        )


async def _cancelled_observer_privacy_probe() -> BaseException:
    global _privacy_capture

    token = "snapshot-private-token-812"
    symbol = "005930"
    observed_at = datetime(2026, 8, 12, 6, 20, tzinfo=UTC)
    original_error = asyncio.CancelledError("snapshot-private-cancellation-812")
    transport = ScriptedTransport([original_error])
    public_error: BaseException | None = None
    try:
        with pytest.raises(TossIncompleteHlitKrxMarketSafetySnapshot) as raised:
            await TossHlitKrxMarketSafetyReadOnlyObserver(
                transport=transport
            ).read_snapshot(symbol=symbol, observed_at=observed_at, access_token=token)
        public_error = raised.value
        _privacy_capture = _PrivacyCapture(
            forbidden=(
                token,
                symbol,
                observed_at,
                *transport.requests,
                transport,
                original_error,
            ),
            private_contents=(
                token,
                symbol,
                observed_at.isoformat(),
                str(original_error),
            ),
            request_count=len(transport.requests),
        )
        assert public_error is not None
        del raised
        return public_error
    finally:
        del token, symbol, observed_at, original_error, transport, public_error


@pytest.mark.asyncio
async def test_incomplete_snapshot_public_error_retains_no_private_graph() -> None:
    raised = await _observer_privacy_probe()
    capture = _privacy_capture
    assert capture is not None

    assert capture.request_count == 2
    assert raised.__cause__ is None
    assert raised.__context__ is None
    reachable = tuple(_error_reachable_values(raised))
    assert any(
        isinstance(value, FrameType)
        and value.f_code.co_filename.endswith("hlit_market_safety.py")
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


@pytest.mark.asyncio
async def test_cancelled_vi_public_error_retains_no_private_graph() -> None:
    raised = await _cancelled_observer_privacy_probe()
    capture = _privacy_capture
    assert capture is not None

    assert capture.request_count == 1
    assert raised.__cause__ is None
    assert raised.__context__ is None
    reachable = tuple(_error_reachable_values(raised))
    assert all(
        all(value is not forbidden for value in reachable)
        for forbidden in capture.forbidden
    )
    assert all(
        not _contains_private_content(value, capture.private_contents)
        for value in reachable
    )


def _error_reachable_values(error: BaseException) -> Iterator[object]:
    pending: list[object] = [error]
    visited: set[int] = set()
    while pending and len(visited) < 1_000:
        value = pending.pop()
        if id(value) in visited:
            continue
        visited.add(id(value))
        yield value
        if isinstance(value, BaseException):
            pending.extend(
                (value.args, value.__cause__, value.__context__, value.__traceback__)
            )
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
        elif isinstance(value, MethodType):
            pending.extend((value.__self__, value.__func__))
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
