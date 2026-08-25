from __future__ import annotations

import ast
import asyncio
import json
import reprlib
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import FrameType, FunctionType, MethodType, TracebackType
from typing import cast

import pytest

from autotrader.integrations.brokers.common import BrokerRequest, BrokerResponse
from autotrader.integrations.brokers.kis.account_snapshot import (
    KisDomesticCashEnvironment,
    KisIncompleteAccountSnapshot,
    KisKrDomesticCashPosition,
    KisStableKrDomesticCashAccountSnapshot,
    collect_stable_kis_kr_domestic_cash_account_snapshot,
)
from autotrader.integrations.brokers.kis.domestic_cash import KisDomesticCashAccount
from autotrader.integrations.brokers.kis.read_contracts import KisReadCredentials

EVIDENCE = (
    Path(__file__).resolve().parents[5] / "docs/providers/kis/"
    "b093e42-domestic-cash-account-snapshot.sanitized.json"
)
MODULE = "autotrader.integrations.brokers.kis.account_snapshot"
SOURCE = (
    Path(__file__).resolve().parents[5]
    / "src/autotrader/integrations/brokers/kis/account_snapshot.py"
)
OBSERVED_AT = datetime(2026, 8, 19, 5, 30, tzinfo=UTC)


class ScriptedTransport:
    def __init__(self, responses: Sequence[BrokerResponse | BaseException]) -> None:
        self.responses = list(responses)
        self.requests: list[BrokerRequest] = []

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected provider request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _credentials() -> KisReadCredentials:
    return KisReadCredentials(
        access_token="account-token", app_key="account-key", app_secret="account-secret"
    )


def _account() -> KisDomesticCashAccount:
    return KisDomesticCashAccount(account_number="81012345", product_code="01")


def _holding(symbol: str, total: str, sellable: str) -> dict[str, object]:
    return {"pdno": symbol, "hldg_qty": total, "ord_psbl_qty": sellable}


def _balance_response(
    *holdings: dict[str, object],
    cash: str = "5000000",
    continuation: str | None = None,
    cursor: tuple[str, str] | None = None,
) -> BrokerResponse:
    payload: dict[str, object] = {
        "rt_cd": "0",
        "output1": list(holdings),
        "output2": [{"dnca_tot_amt": cash}],
        "ctx_area_fk100": "" if cursor is None else cursor[0],
        "ctx_area_nk100": "" if cursor is None else cursor[1],
    }
    headers = () if continuation is None else (("tr_cont", continuation),)
    return BrokerResponse(
        status=200,
        headers=headers,
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )


def _stable_single_page_responses() -> list[BrokerResponse]:
    scan = _balance_response(
        _holding("005930", "100", "90"),
        _holding("000660", "20", "15"),
        _holding("035420", "0", "0"),
    )
    return [scan, scan]


def test_sanitized_official_evidence_pins_account_snapshot_meanings() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    assert evidence["source"] == {
        "repository": "https://github.com/koreainvestment/open-trading-api",
        "revision": "b093e42ba32d1df5f5ddad7a71cb715cbc800832",
        "capturedAt": "2026-08-18",
        "files": [
            {
                "path": (
                    "examples_llm/domestic_stock/inquire_balance/inquire_balance.py"
                ),
                "sha256": (
                    "32f2759f925dfaf9ac710bc7caf3447f31da83d0a3decdee9b02791a3f0cdfe5"
                ),
            },
            {
                "path": (
                    "examples_llm/domestic_stock/inquire_balance/chk_inquire_balance.py"
                ),
                "sha256": (
                    "5897fd3ce320a8d9683208689727714c037241b2010cc89f4c7e6c63b6255c89"
                ),
            },
        ],
    }
    assert evidence["balanceRead"] == {
        "method": "GET",
        "path": "/uapi/domestic-stock/v1/trading/inquire-balance",
        "realTrId": "TTTC8434R",
        "paperTrId": "VTTC8434R",
        "requestParameters": [
            "CANO",
            "ACNT_PRDT_CD",
            "AFHR_FLPR_YN",
            "OFL_YN",
            "INQR_DVSN",
            "UNPR_DVSN",
            "FUND_STTL_ICLD_YN",
            "FNCG_AMT_AUTO_RDPT_YN",
            "PRCS_DVSN",
            "CTX_AREA_FK100",
            "CTX_AREA_NK100",
        ],
        "realPageLimit": 50,
        "paperPageLimit": 20,
        "continuationResponseHeaders": ["M", "F"],
        "terminalBehavior": {
            "liveObservedResponseHeaders": ["D", "E"],
            "observedAt": "2026-08-19",
            "officialPinnedSampleContinuesOnlyOn": ["M", "F"],
            "unknownHeadersAcceptedByCollector": False,
        },
        "continuationRequestHeader": {"tr_cont": "N"},
        "cursorFields": ["ctx_area_fk100", "ctx_area_nk100"],
    }
    assert evidence["authorityProjection"] == {
        "output1": {
            "pdno": "DOMESTIC_PRODUCT_NUMBER",
            "hldg_qty": "TOTAL_HOLDING_QUANTITY",
            "ord_psbl_qty": "ORDER_AVAILABLE_QUANTITY",
        },
        "output2": {"dnca_tot_amt": "TOTAL_DEPOSIT_CASH"},
        "capture": "TWO_CONSECUTIVE_EQUAL_AUTHORITY_RELEVANT_PROJECTIONS",
        "providerAtomicSnapshotClaimed": False,
        "observationTime": "CAPTURED_BEFORE_FIRST_PROVIDER_READ",
        "returnedScope": "KR_DOMESTIC_SIX_DIGIT_ACCOUNT_PRODUCTS",
        "krxCommonStockClassification": "BLOCKED_MISSING_INSTRUMENT_AUTHORITY",
        "persistence": "BLOCKED_PROVIDER_EVIDENCE",
    }


@pytest.mark.asyncio
async def test_collects_two_equal_complete_account_projections() -> None:
    transport = ScriptedTransport(_stable_single_page_responses())

    snapshot = await collect_stable_kis_kr_domestic_cash_account_snapshot(
        transport=transport,
        environment=KisDomesticCashEnvironment.REAL,
        credentials=_credentials(),
        account=_account(),
        max_pages=2,
        clock=lambda: OBSERVED_AT,
    )

    assert snapshot == KisStableKrDomesticCashAccountSnapshot(
        observed_at=OBSERVED_AT,
        environment=KisDomesticCashEnvironment.REAL,
        total_deposit_cash=Decimal("5000000"),
        positions=(
            snapshot.positions[0],
            snapshot.positions[1],
        ),
        source_hash=snapshot.source_hash,
    )
    assert [position.symbol for position in snapshot.positions] == ["000660", "005930"]
    assert [position.total_quantity for position in snapshot.positions] == [
        Decimal("20"),
        Decimal("100"),
    ]
    assert [position.order_available_quantity for position in snapshot.positions] == [
        Decimal("15"),
        Decimal("90"),
    ]
    assert snapshot.scope == "KR_DOMESTIC_SIX_DIGIT_ACCOUNT_PRODUCTS"
    assert snapshot.environment is KisDomesticCashEnvironment.REAL
    assert snapshot.cash_meaning == "TOTAL_DEPOSIT_CASH"
    assert snapshot.observed_at is OBSERVED_AT
    assert snapshot.source_hash.hex() == (
        "432f1c33181cc73bb62e47944e76328ebaef47f2a53028b502999d9a06ab1470"
    )
    assert len(transport.requests) == 2
    expected_path = (
        "/uapi/domestic-stock/v1/trading/inquire-balance?"
        "CANO=81012345&ACNT_PRDT_CD=01&AFHR_FLPR_YN=N&OFL_YN="
        "&INQR_DVSN=02&UNPR_DVSN=01&FUND_STTL_ICLD_YN=N"
        "&FNCG_AMT_AUTO_RDPT_YN=N&PRCS_DVSN=00"
        "&CTX_AREA_FK100=&CTX_AREA_NK100="
    )
    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path=expected_path,
            headers=(
                ("authorization", "Bearer account-token"),
                ("appkey", "account-key"),
                ("appsecret", "account-secret"),
                ("tr_id", "TTTC8434R"),
                ("custtype", "P"),
            ),
        ),
        BrokerRequest(
            method="GET",
            path=expected_path,
            headers=(
                ("authorization", "Bearer account-token"),
                ("appkey", "account-key"),
                ("appsecret", "account-secret"),
                ("tr_id", "TTTC8434R"),
                ("custtype", "P"),
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_captures_observation_time_once_before_the_first_provider_read() -> None:
    transport = ScriptedTransport(_stable_single_page_responses())
    clock_calls = 0

    def clock() -> datetime:
        nonlocal clock_calls
        assert transport.requests == []
        clock_calls += 1
        return OBSERVED_AT

    snapshot = await collect_stable_kis_kr_domestic_cash_account_snapshot(
        transport=transport,
        environment=KisDomesticCashEnvironment.REAL,
        credentials=_credentials(),
        account=_account(),
        max_pages=1,
        clock=clock,
    )

    assert clock_calls == 1
    assert snapshot.observed_at is OBSERVED_AT


@pytest.mark.asyncio
async def test_paper_environment_uses_only_the_official_paper_read_tr_id() -> None:
    transport = ScriptedTransport(_stable_single_page_responses())

    snapshot = await collect_stable_kis_kr_domestic_cash_account_snapshot(
        transport=transport,
        environment=KisDomesticCashEnvironment.PAPER,
        credentials=_credentials(),
        account=_account(),
        max_pages=1,
    )

    assert len(transport.requests) == 2
    assert all(
        ("tr_id", "VTTC8434R") in request.headers for request in transport.requests
    )
    assert snapshot.environment is KisDomesticCashEnvironment.PAPER
    assert snapshot.source_hash != bytes.fromhex(
        "432f1c33181cc73bb62e47944e76328ebaef47f2a53028b502999d9a06ab1470"
    )


@pytest.mark.asyncio
async def test_accepts_a_stable_empty_cash_account_projection() -> None:
    response = _balance_response(cash="0")

    snapshot = await collect_stable_kis_kr_domestic_cash_account_snapshot(
        transport=ScriptedTransport([response, response]),
        environment=KisDomesticCashEnvironment.REAL,
        credentials=_credentials(),
        account=_account(),
        max_pages=1,
    )

    assert snapshot.total_deposit_cash == 0
    assert snapshot.positions == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", ["D", "E"])
async def test_accepts_observed_terminal_account_page_headers(
    continuation: str,
) -> None:
    response = _balance_response(
        continuation=continuation, cursor=("LIVE-FK", "LIVE-NK")
    )
    transport = ScriptedTransport([response, response])

    snapshot = await collect_stable_kis_kr_domestic_cash_account_snapshot(
        transport=transport,
        environment=KisDomesticCashEnvironment.REAL,
        credentials=_credentials(),
        account=_account(),
        max_pages=1,
    )

    assert snapshot.positions == ()
    assert snapshot.total_deposit_cash == Decimal("5000000")
    assert len(transport.requests) == 2
    assert all(
        ("tr_cont", "N") not in request.headers for request in transport.requests
    )


@pytest.mark.parametrize(
    ("environment", "source_hash"),
    [
        (KisDomesticCashEnvironment.REAL, b"x" * 32),
        (
            KisDomesticCashEnvironment.PAPER,
            bytes.fromhex(
                "432f1c33181cc73bb62e47944e76328ebaef47f2a53028b502999d9a06ab1470"
            ),
        ),
    ],
)
def test_snapshot_rejects_a_noncanonical_or_wrong_environment_hash(
    environment: KisDomesticCashEnvironment, source_hash: bytes
) -> None:
    with pytest.raises(ValueError, match="KIS stable account snapshot is invalid"):
        KisStableKrDomesticCashAccountSnapshot(
            observed_at=OBSERVED_AT,
            environment=environment,
            total_deposit_cash=Decimal("5000000"),
            positions=(
                KisKrDomesticCashPosition(
                    symbol="000660",
                    total_quantity=Decimal("20"),
                    order_available_quantity=Decimal("15"),
                ),
                KisKrDomesticCashPosition(
                    symbol="005930",
                    total_quantity=Decimal("100"),
                    order_available_quantity=Decimal("90"),
                ),
            ),
            source_hash=source_hash,
        )


def test_snapshot_canonicalizes_integral_decimal_exponents_before_hashing() -> None:
    position = KisKrDomesticCashPosition(
        symbol="005930",
        total_quantity=Decimal("100.0"),
        order_available_quantity=Decimal("90.000"),
    )
    snapshot = KisStableKrDomesticCashAccountSnapshot(
        observed_at=OBSERVED_AT,
        environment=KisDomesticCashEnvironment.REAL,
        total_deposit_cash=Decimal("0E-1000"),
        positions=(),
        source_hash=bytes.fromhex(
            "ff343f71e37e907ccde96ea3f6781de1a7da353515e1a4f6b9bf6add9fdf2c62"
        ),
    )

    assert position.total_quantity.as_tuple().exponent == 0
    assert position.order_available_quantity.as_tuple().exponent == 0
    assert snapshot.total_deposit_cash.as_tuple().exponent == 0


@pytest.mark.parametrize(
    "observed_at",
    [
        datetime(2026, 8, 19, 5, 30),
        datetime(2026, 8, 19, 14, 30, tzinfo=timezone(timedelta(hours=9))),
        datetime(2026, 8, 19, 5, 30, 0, 1, tzinfo=UTC),
    ],
)
def test_snapshot_constructor_rejects_non_exact_utc_observation_time(
    observed_at: datetime,
) -> None:
    with pytest.raises(ValueError, match="KIS stable account snapshot is invalid"):
        KisStableKrDomesticCashAccountSnapshot(
            observed_at=observed_at,
            environment=KisDomesticCashEnvironment.REAL,
            total_deposit_cash=Decimal(0),
            positions=(),
            source_hash=bytes.fromhex(
                "ff343f71e37e907ccde96ea3f6781de1a7da353515e1a4f6b9bf6add9fdf2c62"
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "observed_at",
    [
        datetime(2026, 8, 19, 5, 30),
        datetime(2026, 8, 19, 5, 30, 0, 1, tzinfo=UTC),
    ],
)
async def test_rejects_invalid_observation_time_before_provider_reads(
    observed_at: datetime,
) -> None:
    transport = ScriptedTransport([])

    with pytest.raises(KisIncompleteAccountSnapshot):
        await collect_stable_kis_kr_domestic_cash_account_snapshot(
            transport=transport,
            environment=KisDomesticCashEnvironment.REAL,
            credentials=_credentials(),
            account=_account(),
            max_pages=1,
            clock=lambda: observed_at,
        )

    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", ["M", "F"])
async def test_follows_documented_continuation_headers(continuation: str) -> None:
    page1 = _balance_response(
        _holding("005930", "100", "90"),
        continuation=continuation,
        cursor=("NEXT-FK", "NEXT-NK"),
    )
    page2 = _balance_response(_holding("000660", "20", "15"))
    transport = ScriptedTransport([page1, page2, page1, page2])

    snapshot = await collect_stable_kis_kr_domestic_cash_account_snapshot(
        transport=transport,
        environment=KisDomesticCashEnvironment.REAL,
        credentials=_credentials(),
        account=_account(),
        max_pages=2,
    )

    assert [position.symbol for position in snapshot.positions] == ["000660", "005930"]
    assert ("tr_cont", "N") in transport.requests[1].headers
    assert transport.requests[1].path.endswith(
        "CTX_AREA_FK100=NEXT-FK&CTX_AREA_NK100=NEXT-NK"
    )
    assert transport.requests[3] == transport.requests[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses",
    [
        [_balance_response(_holding("005930", "100", "101"))] * 2,
        [
            _balance_response(
                _holding("005930", "100", "90"),
                _holding("005930", "100", "90"),
            )
        ]
        * 2,
        [_balance_response(_holding("005930", "1.5", "1"))] * 2,
        [_balance_response(_holding("005930", "1", "-1"))] * 2,
        [BrokerResponse(status=500, body=b"provider-private")] * 2,
    ],
)
async def test_rejects_invalid_provider_projections(
    responses: list[BrokerResponse],
) -> None:
    transport = ScriptedTransport(responses)

    with pytest.raises(
        KisIncompleteAccountSnapshot, match="KIS account snapshot is incomplete"
    ) as raised:
        await collect_stable_kis_kr_domestic_cash_account_snapshot(
            transport=transport,
            environment=KisDomesticCashEnvironment.REAL,
            credentials=_credentials(),
            account=_account(),
            max_pages=2,
        )

    assert raised.value.args == ("KIS account snapshot is incomplete",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
async def test_rejects_cash_change_between_pages() -> None:
    page1 = _balance_response(
        _holding("005930", "100", "90"),
        continuation="M",
        cursor=("NEXT-FK", "NEXT-NK"),
    )
    page2 = _balance_response(_holding("000660", "20", "15"), cash="4999999")

    with pytest.raises(KisIncompleteAccountSnapshot):
        await collect_stable_kis_kr_domestic_cash_account_snapshot(
            transport=ScriptedTransport([page1, page2]),
            environment=KisDomesticCashEnvironment.REAL,
            credentials=_credentials(),
            account=_account(),
            max_pages=2,
        )


@pytest.mark.asyncio
async def test_rejects_projection_change_between_complete_passes() -> None:
    first = _balance_response(_holding("005930", "100", "90"))
    second = _balance_response(_holding("005930", "100", "89"))

    with pytest.raises(KisIncompleteAccountSnapshot):
        await collect_stable_kis_kr_domestic_cash_account_snapshot(
            transport=ScriptedTransport([first, second]),
            environment=KisDomesticCashEnvironment.REAL,
            credentials=_credentials(),
            account=_account(),
            max_pages=1,
        )


@pytest.mark.asyncio
async def test_rejects_page_exhaustion_and_repeated_cursor() -> None:
    continued = _balance_response(
        _holding("005930", "100", "90"),
        continuation="M",
        cursor=("NEXT-FK", "NEXT-NK"),
    )
    with pytest.raises(KisIncompleteAccountSnapshot):
        await collect_stable_kis_kr_domestic_cash_account_snapshot(
            transport=ScriptedTransport([continued]),
            environment=KisDomesticCashEnvironment.REAL,
            credentials=_credentials(),
            account=_account(),
            max_pages=1,
        )

    repeated = _balance_response(
        _holding("000660", "20", "15"),
        continuation="F",
        cursor=("NEXT-FK", "NEXT-NK"),
    )
    with pytest.raises(KisIncompleteAccountSnapshot):
        await collect_stable_kis_kr_domestic_cash_account_snapshot(
            transport=ScriptedTransport([continued, repeated]),
            environment=KisDomesticCashEnvironment.REAL,
            credentials=_credentials(),
            account=_account(),
            max_pages=3,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", ["N", "X", " "])
async def test_rejects_undocumented_nonempty_terminal_headers(
    continuation: str,
) -> None:
    response = _balance_response(
        _holding("005930", "100", "90"), continuation=continuation
    )
    with pytest.raises(KisIncompleteAccountSnapshot):
        await collect_stable_kis_kr_domestic_cash_account_snapshot(
            transport=ScriptedTransport([response]),
            environment=KisDomesticCashEnvironment.REAL,
            credentials=_credentials(),
            account=_account(),
            max_pages=1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("max_pages", [0, True, "2"])
async def test_rejects_invalid_input_before_transport(max_pages: object) -> None:
    transport = ScriptedTransport([])
    with pytest.raises(KisIncompleteAccountSnapshot):
        await collect_stable_kis_kr_domestic_cash_account_snapshot(
            transport=transport,
            environment=KisDomesticCashEnvironment.REAL,
            credentials=_credentials(),
            account=_account(),
            max_pages=max_pages,
        )
    assert transport.requests == []


@pytest.mark.asyncio
async def test_rejects_non_cash_product_code_before_transport() -> None:
    transport = ScriptedTransport([])

    with pytest.raises(KisIncompleteAccountSnapshot):
        await collect_stable_kis_kr_domestic_cash_account_snapshot(
            transport=transport,
            environment=KisDomesticCashEnvironment.REAL,
            credentials=_credentials(),
            account=KisDomesticCashAccount(
                account_number="81012345", product_code="08"
            ),
            max_pages=1,
        )

    assert transport.requests == []


def test_module_ast_has_no_operational_imports() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imports = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    forbidden = (
        "autotrader.application",
        "autotrader.apps",
        "autotrader.execution",
        "autotrader.operations",
        "autotrader.persistence",
        "autotrader.scheduler",
    )
    assert not any(module.startswith(forbidden) for module in imports)


def test_fresh_import_loads_no_operational_modules() -> None:
    code = f"""
import importlib
import sys
importlib.import_module({MODULE!r})
forbidden = (
    'autotrader.application', 'autotrader.apps', 'autotrader.execution',
    'autotrader.operations', 'autotrader.persistence', 'autotrader.scheduler',
)
loaded = sorted(name for name in sys.modules if name.startswith(forbidden))
if loaded:
    raise SystemExit(';'.join(loaded))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=Path(__file__).resolve().parents[5],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@dataclass(frozen=True, slots=True)
class _PrivacyCapture:
    forbidden: tuple[object, ...]
    private_contents: tuple[str, ...]
    request_count: int


_privacy_capture: _PrivacyCapture | None = None


async def _provider_failure_privacy_probe() -> BaseException:
    global _privacy_capture
    credentials = KisReadCredentials(
        access_token="private-account-token-711",
        app_key="private-account-key-712",
        app_secret="private-account-secret-713",
    )
    account = KisDomesticCashAccount(account_number="98765431", product_code="01")
    raw = bytes(bytearray(b'{"private-provider-payload":"snapshot-714"}'))
    response = BrokerResponse(status=200, body=raw)
    transport = ScriptedTransport([response])
    request: BrokerRequest | None = None
    public_error: BaseException | None = None
    try:
        with pytest.raises(KisIncompleteAccountSnapshot) as raised:
            await collect_stable_kis_kr_domestic_cash_account_snapshot(
                transport=transport,
                environment=KisDomesticCashEnvironment.REAL,
                credentials=credentials,
                account=account,
                max_pages=1,
            )
        public_error = raised.value
        request = transport.requests[0]
        _privacy_capture = _PrivacyCapture(
            forbidden=(credentials, account, raw, response, request, transport),
            private_contents=(
                credentials.access_token,
                credentials.app_key,
                credentials.app_secret,
                raw.decode("utf-8"),
                account.account_number,
            ),
            request_count=len(transport.requests),
        )
        return public_error
    finally:
        del credentials, account, raw, response, transport, request, public_error


async def _transport_failure_privacy_probe() -> BaseException:
    global _privacy_capture
    credentials = KisReadCredentials(
        access_token="private-account-token-721",
        app_key="private-account-key-722",
        app_secret="private-account-secret-723",
    )
    account = KisDomesticCashAccount(account_number="98765432", product_code="01")
    transport_error = OSError("private-transport-724")
    transport = ScriptedTransport([transport_error])
    request: BrokerRequest | None = None
    public_error: BaseException | None = None
    try:
        with pytest.raises(KisIncompleteAccountSnapshot) as raised:
            await collect_stable_kis_kr_domestic_cash_account_snapshot(
                transport=transport,
                environment=KisDomesticCashEnvironment.REAL,
                credentials=credentials,
                account=account,
                max_pages=1,
            )
        public_error = raised.value
        request = transport.requests[0]
        _privacy_capture = _PrivacyCapture(
            forbidden=(
                credentials,
                account,
                transport_error,
                request,
                transport,
            ),
            private_contents=(
                credentials.access_token,
                credentials.app_key,
                credentials.app_secret,
                "private-transport-724",
                account.account_number,
            ),
            request_count=len(transport.requests),
        )
        return public_error
    finally:
        del (
            credentials,
            account,
            transport_error,
            transport,
            request,
            public_error,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "factory", (_provider_failure_privacy_probe, _transport_failure_privacy_probe)
)
async def test_public_failures_retain_no_account_snapshot_secrets(
    factory: Callable[[], Awaitable[BaseException]],
) -> None:
    public_error = await factory()
    capture = _privacy_capture
    assert capture is not None
    assert type(public_error) is KisIncompleteAccountSnapshot
    assert public_error.args == ("KIS account snapshot is incomplete",)
    assert public_error.__cause__ is None
    assert public_error.__context__ is None
    assert capture.request_count == 1
    reachable = tuple(_error_reachable_values(public_error))
    assert any(isinstance(value, FrameType) for value in reachable)
    assert all(
        all(value is not forbidden for value in reachable)
        for forbidden in capture.forbidden
    )
    assert all(
        not _contains_private_content(value, capture.private_contents)
        for value in reachable
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control_type", (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
)
async def test_control_failures_propagate_exact_sanitized_object(
    control_type: (
        type[asyncio.CancelledError] | type[KeyboardInterrupt] | type[SystemExit]
    ),
) -> None:
    credentials = KisReadCredentials(
        access_token="private-control-token-731",
        app_key="private-control-key-732",
        app_secret="private-control-secret-733",
    )
    account = KisDomesticCashAccount(account_number="98765433", product_code="01")
    control = control_type("private-control-734")
    transport = ScriptedTransport([control])

    with pytest.raises(control_type) as raised:
        await collect_stable_kis_kr_domestic_cash_account_snapshot(
            transport=transport,
            environment=KisDomesticCashEnvironment.REAL,
            credentials=credentials,
            account=account,
            max_pages=1,
        )

    assert raised.value is control
    assert raised.value.args == ()
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    if isinstance(raised.value, SystemExit):
        assert raised.value.code == 1
    request = transport.requests[0]
    reachable = tuple(_error_reachable_values(raised.value))
    assert all(
        value is not forbidden
        for value in reachable
        for forbidden in (credentials, account, request, transport)
    )
    private_contents = (
        credentials.access_token,
        credentials.app_key,
        credentials.app_secret,
        "private-control-734",
        account.account_number,
    )
    assert all(
        not _contains_private_content(value, private_contents) for value in reachable
    )


def _error_reachable_values(error: BaseException) -> Iterator[object]:
    pending: list[object] = [error]
    visited: set[int] = set()
    while pending and len(visited) < 750:
        value = pending.pop()
        if value is None or id(value) in visited:
            continue
        visited.add(id(value))
        yield value
        if isinstance(value, BaseException):
            pending.extend(value.args)
            pending.extend((value.__cause__, value.__context__, value.__traceback__))
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
                raw_slots = owner.__dict__.get("__slots__")
                slots = (raw_slots,) if isinstance(raw_slots, str) else raw_slots
                if not isinstance(slots, tuple):
                    continue
                for slot in cast(tuple[object, ...], slots):
                    if isinstance(slot, str) and hasattr(value, slot):
                        pending.append(getattr(value, slot))


def _contains_private_content(value: object, contents: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(content in value for content in contents)
    if isinstance(value, bytes):
        return any(content.encode("utf-8") in value for content in contents)
    renderer = reprlib.Repr()
    renderer.maxother = 1_024
    renderer.maxstring = 1_024
    try:
        rendered = renderer.repr(value)
    except Exception:
        return False
    return any(content in rendered for content in contents)
