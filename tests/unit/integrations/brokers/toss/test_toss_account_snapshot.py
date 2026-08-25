from __future__ import annotations

import ast
import json
import reprlib
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import FrameType, FunctionType, MethodType, TracebackType
from typing import cast

import pytest

from autotrader.integrations.brokers.common import BrokerRequest, BrokerResponse
from autotrader.integrations.brokers.toss.account_snapshot import (
    TossHoldingPosition,
    TossKrDomesticSellablePosition,
    TossStableKrDomesticCashAccountSnapshot,
    collect_stable_toss_kr_domestic_cash_account_snapshot,
    decode_toss_holdings,
    decode_toss_sellable_quantity,
)
from autotrader.integrations.brokers.toss.adapter import (
    TossAccount,
    TossIncompleteAccountSnapshot,
    TossReadOnlyAdapter,
)

EVIDENCE = (
    Path(__file__).resolve().parents[5] / "docs/providers/toss/"
    "openapi-1.2.14-account-snapshot-contract.sanitized.json"
)
OBSERVED_AT = datetime(2026, 8, 19, 1, 2, 3, tzinfo=UTC)


class RecordingTransport:
    def __init__(self, response: BrokerResponse) -> None:
        self.response = response
        self.requests: list[BrokerRequest] = []

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        return self.response


class ScriptedTransport:
    def __init__(self, responses: list[BrokerResponse | BaseException]) -> None:
        self.responses = responses
        self.requests: list[BrokerRequest] = []

    async def request(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected provider read")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class ScriptedAccountReader:
    def __init__(self, responses: list[BrokerResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, object]] = []

    def _next(self) -> BrokerResponse:
        if not self.responses:
            raise AssertionError("unexpected provider read")
        return self.responses.pop(0)

    async def read_holdings(self, **kwargs: object) -> BrokerResponse:
        self.calls.append(("holdings", kwargs))
        return self._next()

    async def read_krw_cash_buying_power(self, **kwargs: object) -> BrokerResponse:
        self.calls.append(("buying_power", kwargs))
        return self._next()

    async def read_sellable_quantity(self, **kwargs: object) -> BrokerResponse:
        self.calls.append(("sellable_quantity", kwargs))
        return self._next()


def _holding_item(
    *, symbol: str, market_country: str, currency: str, quantity: str
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "name": f"name-{symbol}",
        "marketCountry": market_country,
        "currency": currency,
        "quantity": quantity,
        "lastPrice": "70000",
        "averagePurchasePrice": "65000",
        "marketValue": {
            "purchaseAmount": "65000",
            "amount": "70000",
            "amountAfterCost": "69900",
        },
        "profitLoss": {
            "amount": "5000",
            "amountAfterCost": "4900",
            "rate": "0.0769",
            "rateAfterCost": "0.0753",
        },
        "dailyProfitLoss": {"amount": "100", "rate": "0.0014"},
        "cost": {"commission": "10", "tax": None},
    }


def _holdings_response(*items: dict[str, object]) -> BrokerResponse:
    body: dict[str, object] = {
        "result": {
            "totalPurchaseAmount": {"krw": "65000", "usd": None},
            "marketValue": {
                "amount": {"krw": "70000", "usd": None},
                "amountAfterCost": {"krw": "69900", "usd": None},
            },
            "profitLoss": {
                "amount": {"krw": "5000", "usd": None},
                "amountAfterCost": {"krw": "4900", "usd": None},
                "rate": "0.0769",
                "rateAfterCost": "0.0753",
            },
            "dailyProfitLoss": {
                "amount": {"krw": "100", "usd": None},
                "rate": "0.0014",
            },
            "items": list(items),
        }
    }
    return BrokerResponse(
        status=200,
        body=json.dumps(body, separators=(",", ":")).encode("utf-8"),
    )


def _buying_power_response(amount: str) -> BrokerResponse:
    return BrokerResponse(
        status=200,
        body=json.dumps(
            {"result": {"currency": "KRW", "cashBuyingPower": amount}},
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def _sellable_response(quantity: str) -> BrokerResponse:
    return BrokerResponse(
        status=200,
        body=json.dumps(
            {"result": {"sellableQuantity": quantity}}, separators=(",", ":")
        ).encode("utf-8"),
    )


def _stable_provider_responses() -> list[BrokerResponse]:
    one_scan = [
        _holdings_response(
            _holding_item(
                symbol="005930",
                market_country="KR",
                currency="KRW",
                quantity="100",
            ),
            _holding_item(
                symbol="000660",
                market_country="KR",
                currency="KRW",
                quantity="20",
            ),
            _holding_item(
                symbol="AAPL",
                market_country="US",
                currency="USD",
                quantity="1.5",
            ),
        ),
        _buying_power_response("5000000"),
        _sellable_response("15"),
        _sellable_response("90"),
    ]
    return [*one_scan, *one_scan]


def test_sanitized_openapi_evidence_pins_account_snapshot_meanings() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    assert evidence["source"] == {
        "url": "https://openapi.tossinvest.com/openapi-docs/latest/openapi.json",
        "capturedAt": "2026-08-18",
        "openapi": "3.1.0",
        "version": "1.2.14",
        "sha256": ("d29f9079a557c0b6affcec330aa131f93b09fd49932354668e3dc4524cd42180"),
    }
    holdings = cast(dict[str, object], evidence["holdings"])
    assert holdings == {
        "method": "GET",
        "path": "/api/v1/holdings",
        "accountHeader": "X-Tossinvest-Account",
        "optionalQuery": ["symbol"],
        "omittedSymbolReturns": "ALL_KR_AND_US_HOLDINGS",
        "paginationParameters": [],
        "resultSchema": "HoldingsOverview",
        "resultRequired": [
            "totalPurchaseAmount",
            "marketValue",
            "profitLoss",
            "dailyProfitLoss",
            "items",
        ],
        "itemSchema": "HoldingsItem",
        "itemRequired": [
            "symbol",
            "name",
            "marketCountry",
            "currency",
            "quantity",
            "lastPrice",
            "averagePurchasePrice",
            "marketValue",
            "profitLoss",
            "dailyProfitLoss",
            "cost",
        ],
        "nestedRequired": {
            "Price": ["krw"],
            "OverviewMarketValue": ["amount", "amountAfterCost"],
            "OverviewProfitLoss": [
                "amount",
                "amountAfterCost",
                "rate",
                "rateAfterCost",
            ],
            "OverviewDailyProfitLoss": ["amount", "rate"],
            "MarketValue": ["purchaseAmount", "amount", "amountAfterCost"],
            "ProfitLoss": ["amount", "amountAfterCost", "rate", "rateAfterCost"],
            "DailyProfitLoss": ["amount", "rate"],
            "Cost": ["commission"],
        },
        "nestedOptionalNullable": {"Price": ["usd"], "Cost": ["tax"]},
        "quantityMeaning": "TOTAL_HOLDING_QUANTITY",
        "sellableMeaning": False,
    }
    assert evidence["buyingPower"] == {
        "method": "GET",
        "path": "/api/v1/buying-power",
        "requiredQuery": {"currency": "KRW"},
        "resultSchema": "BuyingPowerResponse",
        "resultRequired": ["currency", "cashBuyingPower"],
        "cashBuyingPowerMeaning": "CASH_ONLY_NO_CREDIT_KRW",
    }
    assert evidence["sellableQuantity"] == {
        "method": "GET",
        "path": "/api/v1/sellable-quantity",
        "requiredQuery": ["symbol"],
        "resultSchema": "SellableQuantityResponse",
        "resultRequired": ["sellableQuantity"],
        "quantityMeaning": "SELLABLE_QUANTITY_FOR_ONE_REQUESTED_SYMBOL",
        "batchSupported": False,
        "exchangeProvided": False,
        "securityTypeProvided": False,
    }
    assert evidence["implementationBoundary"] == {
        "capture": "TWO_CONSECUTIVE_EQUAL_AUTHORITY_RELEVANT_PROJECTIONS",
        "providerAtomicSnapshotClaimed": False,
        "totalHoldingSubstitutesForSellable": False,
        "returnedScope": "KR_DOMESTIC_SIX_DIGIT_HOLDINGS",
        "krxCommonStockClassification": "BLOCKED_MISSING_INSTRUMENT_AUTHORITY",
        "persistence": "BLOCKED_PROVIDER_EVIDENCE",
        "blocker": (
            "A complete successful live reconciliation run cannot be produced from "
            "the current Toss order recovery contract."
        ),
    }


def test_holdings_decoder_returns_canonical_mixed_positions() -> None:
    positions = decode_toss_holdings(
        _holdings_response(
            _holding_item(
                symbol="005930",
                market_country="KR",
                currency="KRW",
                quantity="100",
            ),
            _holding_item(
                symbol="AAPL",
                market_country="US",
                currency="USD",
                quantity="1.5",
            ),
        )
    )

    assert positions == (
        TossHoldingPosition(
            symbol="005930",
            market_country="KR",
            currency="KRW",
            total_quantity=Decimal("100"),
        ),
        TossHoldingPosition(
            symbol="AAPL",
            market_country="US",
            currency="USD",
            total_quantity=Decimal("1.5"),
        ),
    )


def test_holdings_decoder_accepts_a_canonical_empty_account() -> None:
    assert decode_toss_holdings(_holdings_response()) == ()


@pytest.mark.parametrize(
    "items",
    (
        (
            _holding_item(
                symbol="005930",
                market_country="KR",
                currency="KRW",
                quantity="1",
            ),
            _holding_item(
                symbol="005930",
                market_country="KR",
                currency="KRW",
                quantity="1",
            ),
        ),
        (
            _holding_item(
                symbol="005930",
                market_country="KR",
                currency="USD",
                quantity="1",
            ),
        ),
        (
            _holding_item(
                symbol="005930",
                market_country="KR",
                currency="KRW",
                quantity="1.5",
            ),
        ),
    ),
)
def test_holdings_decoder_rejects_noncanonical_account_records(
    items: tuple[dict[str, object], ...],
) -> None:
    with pytest.raises(ValueError, match="Toss holdings response is invalid"):
        decode_toss_holdings(_holdings_response(*items))


@pytest.mark.parametrize(
    "response",
    (
        BrokerResponse(status=500, body=b"{}"),
        BrokerResponse(status=200, body=b"not-json"),
        BrokerResponse(status=200, body=b'{"result":{}}'),
    ),
)
def test_holdings_decoder_rejects_incomplete_provider_responses(
    response: BrokerResponse,
) -> None:
    with pytest.raises(ValueError, match="Toss holdings response is invalid"):
        decode_toss_holdings(response)


@pytest.mark.parametrize("field", ("lastPrice", "averagePurchasePrice"))
def test_holdings_decoder_rejects_malformed_required_item_decimals(field: str) -> None:
    item = _holding_item(
        symbol="005930", market_country="KR", currency="KRW", quantity="1"
    )
    item[field] = None

    with pytest.raises(ValueError, match="Toss holdings response is invalid"):
        decode_toss_holdings(_holdings_response(item))


def test_holdings_decoder_rejects_incomplete_required_nested_shapes() -> None:
    item = _holding_item(
        symbol="005930", market_country="KR", currency="KRW", quantity="1"
    )
    item["profitLoss"] = {}
    response = _holdings_response(item)
    payload = cast(dict[str, object], json.loads(response.body))
    result = cast(dict[str, object], payload["result"])
    result["marketValue"] = {}

    with pytest.raises(ValueError, match="Toss holdings response is invalid"):
        decode_toss_holdings(
            BrokerResponse(
                status=200,
                body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            )
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    (("0", Decimal("0")), ("100", Decimal("100"))),
)
def test_sellable_quantity_decoder_returns_domestic_integer_shares(
    raw: str, expected: Decimal
) -> None:
    assert (
        decode_toss_sellable_quantity(
            BrokerResponse(
                status=200,
                body=json.dumps(
                    {"result": {"sellableQuantity": raw}}, separators=(",", ":")
                ).encode("utf-8"),
            )
        )
        == expected
    )


@pytest.mark.parametrize("raw", ("-1", "1.5", "", None, True))
def test_sellable_quantity_decoder_rejects_nonintegral_domestic_quantities(
    raw: object,
) -> None:
    with pytest.raises(ValueError, match="Toss sellable quantity response is invalid"):
        decode_toss_sellable_quantity(
            BrokerResponse(
                status=200,
                body=json.dumps(
                    {"result": {"sellableQuantity": raw}}, separators=(",", ":")
                ).encode("utf-8"),
            )
        )


@pytest.mark.asyncio
async def test_adapter_reads_exact_per_symbol_sellable_quantity_route() -> None:
    response = BrokerResponse(status=200, body=b'{"result":{"sellableQuantity":"1"}}')
    transport = RecordingTransport(response)
    adapter = TossReadOnlyAdapter(transport=transport)

    assert (
        await adapter.read_sellable_quantity(
            access_token="account-token", account_seq=17, symbol="005930"
        )
        is response
    )
    assert transport.requests == [
        BrokerRequest(
            method="GET",
            path="/api/v1/sellable-quantity?symbol=005930",
            headers=(
                ("Authorization", "Bearer account-token"),
                ("X-Tossinvest-Account", "17"),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_collector_requires_two_identical_authority_projections() -> None:
    reader = ScriptedAccountReader(_stable_provider_responses())
    account = TossAccount(account_seq=17, account_type="BROKERAGE")

    snapshot = await collect_stable_toss_kr_domestic_cash_account_snapshot(
        adapter=reader,
        access_token="account-token",
        account=account,
        max_sellable_reads=2,
        clock=lambda: OBSERVED_AT,
    )

    assert snapshot == TossStableKrDomesticCashAccountSnapshot(
        observed_at=OBSERVED_AT,
        cash_buying_power=Decimal("5000000"),
        positions=(
            TossKrDomesticSellablePosition(
                symbol="000660",
                total_quantity=Decimal("20"),
                sellable_quantity=Decimal("15"),
            ),
            TossKrDomesticSellablePosition(
                symbol="005930",
                total_quantity=Decimal("100"),
                sellable_quantity=Decimal("90"),
            ),
        ),
        source_hash=bytes.fromhex(
            "04bba2e14ef228b3cbfa0bd773e326f3ebc1fe9f975daceb818799837eb5175a"
        ),
    )
    assert (
        reader.calls
        == [
            ("holdings", {"access_token": "account-token", "account_seq": 17}),
            ("buying_power", {"access_token": "account-token", "account": account}),
            (
                "sellable_quantity",
                {
                    "access_token": "account-token",
                    "account_seq": 17,
                    "symbol": "000660",
                },
            ),
            (
                "sellable_quantity",
                {
                    "access_token": "account-token",
                    "account_seq": 17,
                    "symbol": "005930",
                },
            ),
        ]
        * 2
    )
    assert reader.responses == []


@pytest.mark.asyncio
async def test_collector_captures_observation_time_before_provider_reads() -> None:
    reader = ScriptedAccountReader(_stable_provider_responses())
    clock_calls = 0

    def clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        assert reader.calls == []
        return OBSERVED_AT

    snapshot = await collect_stable_toss_kr_domestic_cash_account_snapshot(
        adapter=reader,
        access_token="account-token",
        account=TossAccount(account_seq=17, account_type="BROKERAGE"),
        max_sellable_reads=2,
        clock=clock,
    )

    assert clock_calls == 1
    assert snapshot.observed_at is OBSERVED_AT


@pytest.mark.parametrize(
    "observed_at",
    (
        datetime(2026, 8, 19, 1, 2, 3),
        datetime(2026, 8, 19, 1, 2, 3, tzinfo=timezone(timedelta(hours=9))),
        datetime(2026, 8, 19, 1, 2, 3, 1, tzinfo=UTC),
    ),
    ids=("naive", "non-utc-singleton", "microsecond"),
)
def test_snapshot_rejects_non_exact_observation_times(observed_at: datetime) -> None:
    with pytest.raises(ValueError, match="observed"):
        TossStableKrDomesticCashAccountSnapshot(
            observed_at=observed_at,
            cash_buying_power=Decimal("5000000"),
            positions=(),
            source_hash=bytes.fromhex(
                "a8539d4f82abf2a1d1c6c1411541f765434fd9f7aa28287057a909150adf3657"
            ),
        )


@pytest.mark.asyncio
async def test_collector_does_not_claim_stability_for_excluded_us_values() -> None:
    responses = _stable_provider_responses()
    responses[4] = _holdings_response(
        _holding_item(
            symbol="005930", market_country="KR", currency="KRW", quantity="100"
        ),
        _holding_item(
            symbol="000660", market_country="KR", currency="KRW", quantity="20"
        ),
        _holding_item(
            symbol="AAPL", market_country="US", currency="USD", quantity="2.5"
        ),
    )

    snapshot = await collect_stable_toss_kr_domestic_cash_account_snapshot(
        adapter=ScriptedAccountReader(responses),
        access_token="account-token",
        account=TossAccount(account_seq=17, account_type="BROKERAGE"),
        max_sellable_reads=2,
    )

    assert tuple(position.symbol for position in snapshot.positions) == (
        "000660",
        "005930",
    )


@pytest.mark.asyncio
async def test_collector_rejects_changed_consecutive_observations() -> None:
    responses = _stable_provider_responses()
    responses[5] = _buying_power_response("4999999")
    reader = ScriptedAccountReader(responses)

    with pytest.raises(
        TossIncompleteAccountSnapshot, match="Toss account snapshot is incomplete"
    ) as error:
        await collect_stable_toss_kr_domestic_cash_account_snapshot(
            adapter=reader,
            access_token="account-token",
            account=TossAccount(account_seq=17, account_type="BROKERAGE"),
            max_sellable_reads=2,
        )

    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.asyncio
async def test_collector_rejects_sellable_quantity_above_total_holding() -> None:
    responses = _stable_provider_responses()
    responses[2] = _sellable_response("21")
    reader = ScriptedAccountReader(responses)

    with pytest.raises(TossIncompleteAccountSnapshot):
        await collect_stable_toss_kr_domestic_cash_account_snapshot(
            adapter=reader,
            access_token="account-token",
            account=TossAccount(account_seq=17, account_type="BROKERAGE"),
            max_sellable_reads=2,
        )

    assert [name for name, _ in reader.calls] == [
        "holdings",
        "buying_power",
        "sellable_quantity",
    ]


@pytest.mark.asyncio
async def test_collector_rejects_read_budget_before_any_sellable_fanout() -> None:
    reader = ScriptedAccountReader(_stable_provider_responses())

    with pytest.raises(TossIncompleteAccountSnapshot):
        await collect_stable_toss_kr_domestic_cash_account_snapshot(
            adapter=reader,
            access_token="account-token",
            account=TossAccount(account_seq=17, account_type="BROKERAGE"),
            max_sellable_reads=1,
        )

    assert [name for name, _ in reader.calls] == ["holdings"]


@pytest.mark.asyncio
async def test_collector_revalidates_a_forged_account_before_provider_reads() -> None:
    reader = ScriptedAccountReader(_stable_provider_responses())
    account = TossAccount(account_seq=17, account_type="BROKERAGE")
    object.__setattr__(account, "account_seq", -1)

    with pytest.raises(TossIncompleteAccountSnapshot):
        await collect_stable_toss_kr_domestic_cash_account_snapshot(
            adapter=reader,
            access_token="account-token",
            account=account,
            max_sellable_reads=2,
        )

    assert reader.calls == []


@dataclass(frozen=True, slots=True)
class _PrivacyCapture:
    forbidden: tuple[object, ...]
    private_contents: tuple[str, ...]
    request_count: int


_privacy_capture: _PrivacyCapture | None = None


async def _provider_failure_privacy_probe() -> BaseException:
    global _privacy_capture
    token = "".join(("account-private-token", "-901"))
    account = TossAccount(account_seq=987654301, account_type="BROKERAGE")
    raw = bytes(bytearray(b'{"private-provider-payload":"snapshot-902"}'))
    response = BrokerResponse(status=200, body=raw)
    transport = ScriptedTransport([response])
    adapter = TossReadOnlyAdapter(transport=transport)
    request: BrokerRequest | None = None
    public_error: BaseException | None = None
    try:
        with pytest.raises(TossIncompleteAccountSnapshot) as raised:
            await collect_stable_toss_kr_domestic_cash_account_snapshot(
                adapter=adapter,
                access_token=token,
                account=account,
                max_sellable_reads=1,
            )
        public_error = raised.value
        request = transport.requests[0]
        _privacy_capture = _PrivacyCapture(
            forbidden=(
                token,
                account,
                raw,
                request,
                response,
                transport,
                adapter,
            ),
            private_contents=(token, raw.decode("utf-8"), "987654301"),
            request_count=len(transport.requests),
        )
        return public_error
    finally:
        del token, account, raw, response, transport, adapter, request, public_error


async def _transport_failure_privacy_probe() -> BaseException:
    global _privacy_capture
    token = "".join(("account-private-token", "-903"))
    account = TossAccount(account_seq=987654303, account_type="BROKERAGE")
    transport_error = OSError("private-transport-904")
    transport = ScriptedTransport([transport_error])
    adapter = TossReadOnlyAdapter(transport=transport)
    request: BrokerRequest | None = None
    public_error: BaseException | None = None
    try:
        with pytest.raises(TossIncompleteAccountSnapshot) as raised:
            await collect_stable_toss_kr_domestic_cash_account_snapshot(
                adapter=adapter,
                access_token=token,
                account=account,
                max_sellable_reads=1,
            )
        public_error = raised.value
        request = transport.requests[0]
        _privacy_capture = _PrivacyCapture(
            forbidden=(
                token,
                account,
                request,
                transport_error,
                transport,
                adapter,
            ),
            private_contents=(token, "private-transport-904", "987654303"),
            request_count=len(transport.requests),
        )
        return public_error
    finally:
        del (
            token,
            account,
            transport_error,
            transport,
            adapter,
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
    assert type(public_error) is TossIncompleteAccountSnapshot
    assert public_error.args == ("Toss account snapshot is incomplete",)
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
@pytest.mark.parametrize("control_type", (KeyboardInterrupt, SystemExit))
async def test_control_failures_propagate_exact_sanitized_object(
    control_type: type[KeyboardInterrupt] | type[SystemExit],
) -> None:
    token = "".join(("account-control-token", "-905"))
    account = TossAccount(account_seq=987654305, account_type="BROKERAGE")
    control = control_type("private-control-906")
    transport = ScriptedTransport([control])
    adapter = TossReadOnlyAdapter(transport=transport)

    with pytest.raises(control_type) as raised:
        await collect_stable_toss_kr_domestic_cash_account_snapshot(
            adapter=adapter,
            access_token=token,
            account=account,
            max_sellable_reads=1,
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
        for forbidden in (token, account, request, transport, adapter)
    )
    private_contents = (token, "private-control-906", "987654305")
    offenders = tuple(
        value
        for value in reachable
        if _contains_private_content(value, private_contents)
    )
    assert not offenders, tuple(type(value).__name__ for value in offenders)


def test_account_snapshot_import_is_read_only_and_operationally_isolated() -> None:
    module_path = Path("src/autotrader/integrations/brokers/toss/account_snapshot.py")
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    forbidden = (
        "autotrader.execution",
        "autotrader.persistence",
        "autotrader.application",
        "autotrader.apps",
        "autotrader.operations",
        "autotrader.runtime",
        "autotrader.scheduler",
    )
    imported = tuple(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ) + tuple(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    assert all(
        not module.startswith(prefix) for module in imported for prefix in forbidden
    )
    fresh_forbidden = (
        *forbidden,
        "autotrader.integrations.brokers.toss.adapter",
        "autotrader.integrations.brokers.toss.stock_order_contracts",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import autotrader.integrations.brokers.toss.account_snapshot; "
                f"prefixes={fresh_forbidden!r}; "
                "loaded=sorted(name for name in sys.modules "
                "if name.startswith(prefixes)); "
                "assert not loaded, loaded"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


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
