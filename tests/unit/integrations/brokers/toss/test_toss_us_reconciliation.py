from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from autotrader.integrations.brokers.common import BrokerRequest, BrokerResponse
from autotrader.integrations.brokers.toss.rate_limit import TossRateLimitedTransport
from autotrader.integrations.brokers.toss.us_reconciliation import (
    MySqlTossUsReconciliationStore,
    TossUsReconciliationCheckpoint,
    TossUsReconciliationContext,
    TossUsReconciliationResult,
    TossUsReconciliationStore,
    reconcile_toss_us_cash,
)
from autotrader.persistence.mysql.models.toss_us_reconciliation import (
    TossUsCashFactRow,
    TossUsOrderFactRow,
    TossUsPositionFactRow,
    TossUsReconciliationRunRow,
)
from autotrader.persistence.mysql.repositories.toss_us_reconciliation import (
    TossUsReconciliationRepository,
)
from autotrader.shared.ids import new_uuid7

AS_OF = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)
CAPTURED_AT = AS_OF + timedelta(seconds=5)
BINDING_ID = new_uuid7()
ACCOUNT_ID = new_uuid7()
SCOPE = hashlib.sha256(b"disabled-toss-us-binding").digest()


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds

    def now(self) -> datetime:
        return CAPTURED_AT


class _RawTransport:
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


class _Store:
    def __init__(self) -> None:
        self.checkpoint: TossUsReconciliationCheckpoint | None = None
        self.saves: list[TossUsReconciliationCheckpoint] = []
        self.completed: list[TossUsReconciliationResult] = []

    async def load_checkpoint(
        self,
        binding_id: UUID,
        provider_as_of: datetime,
    ) -> TossUsReconciliationCheckpoint | None:
        if self.checkpoint is None:
            return None
        if (
            self.checkpoint.binding_id != binding_id
            or self.checkpoint.provider_as_of != provider_as_of
        ):
            return None
        return self.checkpoint

    async def save_checkpoint(
        self,
        checkpoint: TossUsReconciliationCheckpoint,
    ) -> None:
        self.checkpoint = checkpoint
        self.saves.append(checkpoint)

    async def persist_complete(self, result: TossUsReconciliationResult) -> None:
        self.completed.append(result)
        self.checkpoint = None


class _Repository:
    def __init__(self) -> None:
        self.row: TossUsReconciliationRunRow | None = None
        self.completed: (
            tuple[
                TossUsReconciliationRunRow,
                tuple[TossUsCashFactRow, ...],
                tuple[TossUsPositionFactRow, ...],
                tuple[TossUsOrderFactRow, ...],
            ]
            | None
        ) = None

    async def load_checkpoint(
        self,
        *,
        binding_id: UUID,
        provider_as_of: datetime,
    ) -> TossUsReconciliationRunRow | None:
        if self.row is None:
            return None
        if (
            self.row.binding_id != binding_id
            or self.row.provider_as_of != provider_as_of
        ):
            return None
        return self.row

    async def persist_checkpoint(
        self,
        run: TossUsReconciliationRunRow,
    ) -> TossUsReconciliationRunRow:
        self.row = run
        return run

    async def persist_completed_run(
        self,
        *,
        run: TossUsReconciliationRunRow,
        cash_facts: tuple[TossUsCashFactRow, ...],
        position_facts: tuple[TossUsPositionFactRow, ...],
        order_facts: tuple[TossUsOrderFactRow, ...],
    ) -> TossUsReconciliationRunRow:
        self.completed = (run, cash_facts, position_facts, order_facts)
        return run


def _headers() -> tuple[tuple[str, str], ...]:
    return (
        ("X-RateLimit-Limit", "10"),
        ("X-RateLimit-Remaining", "9"),
        ("X-RateLimit-Reset", "0.1"),
    )


def _response(result: object) -> BrokerResponse:
    return BrokerResponse(
        status=200,
        body=json.dumps({"result": result}, separators=(",", ":")).encode(),
        headers=_headers(),
    )


def _holding() -> dict[str, object]:
    return {
        "symbol": "AAPL",
        "name": "Apple",
        "marketCountry": "US",
        "currency": "USD",
        "quantity": "3",
        "lastPrice": "180",
        "averagePurchasePrice": "100",
        "marketValue": {
            "purchaseAmount": "300",
            "amount": "540",
            "amountAfterCost": "539.5",
        },
        "profitLoss": {
            "amount": "240",
            "amountAfterCost": "239.5",
            "rate": "0.8",
            "rateAfterCost": "0.7983",
        },
        "dailyProfitLoss": {"amount": "1", "rate": "0.002"},
        "cost": {"commission": "0.5", "tax": None},
    }


def _holdings() -> dict[str, object]:
    return {
        "totalPurchaseAmount": {"krw": "0", "usd": "300"},
        "marketValue": {
            "amount": {"krw": "0", "usd": "540"},
            "amountAfterCost": {"krw": "0", "usd": "539.5"},
        },
        "profitLoss": {
            "amount": {"krw": "0", "usd": "240"},
            "amountAfterCost": {"krw": "0", "usd": "239.5"},
            "rate": "0.8",
            "rateAfterCost": "0.7983",
        },
        "dailyProfitLoss": {
            "amount": {"krw": "0", "usd": "1"},
            "rate": "0.002",
        },
        "items": [_holding()],
    }


def _order(*, filled: str) -> dict[str, object]:
    has_fill = filled != "0"
    return {
        "orderId": "open-order-1",
        "symbol": "AAPL",
        "side": "SELL",
        "orderType": "LIMIT",
        "timeInForce": "DAY",
        "status": "PARTIAL_FILLED" if has_fill else "PENDING",
        "price": "181",
        "quantity": "3",
        "orderAmount": None,
        "currency": "USD",
        "orderedAt": "2026-08-24T22:10:00+09:00",
        "canceledAt": None,
        "execution": {
            "filledQuantity": filled,
            "averageFilledPrice": "181" if has_fill else None,
            "filledAmount": "181" if has_fill else None,
            "commission": "0.15" if has_fill else None,
            "tax": "0" if has_fill else None,
            "filledAt": "2026-08-24T22:10:01+09:00" if has_fill else None,
            "settlementDate": None,
        },
    }


def _capture_responses(
    *,
    cash: str = "3500.50",
    filled: str = "1",
) -> list[BrokerResponse]:
    return [
        _response({"currency": "USD", "cashBuyingPower": cash}),
        _response(_holdings()),
        _response({"sellableQuantity": "2.5"}),
        _response(
            {"orders": [_order(filled=filled)], "nextCursor": None, "hasNext": False}
        ),
        _response({"orders": [], "nextCursor": "cursor-2", "hasNext": True}),
        _response({"orders": [], "nextCursor": None, "hasNext": False}),
        _response(
            [
                {
                    "marketCountry": "US",
                    "commissionRate": "0.001",
                    "startDate": None,
                    "endDate": None,
                }
            ]
        ),
    ]


def _context(
    raw: _RawTransport,
    store: TossUsReconciliationStore,
    *,
    new_run_id: Callable[[], UUID] = new_uuid7,
) -> TossUsReconciliationContext:
    clock = _Clock()
    limited = TossRateLimitedTransport(
        transport=raw,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        wall_clock=clock.now,
        jitter=lambda _: 0.0,
        deadline=30.0,
    )
    return TossUsReconciliationContext(
        account_id=ACCOUNT_ID,
        account_scope_digest=SCOPE,
        access_token="private-token",
        account_header="private-account",
        transport=limited,
        store=store,
        clock=clock.now,
        new_run_id=new_run_id,
    )


@pytest.mark.asyncio
async def test_two_equal_full_captures_complete_and_resume_closed_pagination() -> None:
    raw = _RawTransport([*_capture_responses(), *_capture_responses()])
    store = _Store()

    result = await reconcile_toss_us_cash(
        BINDING_ID,
        as_of=AS_OF,
        context=_context(raw, store),
    )

    assert result.state == "COMPLETE"
    assert result.blockers == ()
    assert result.holdings_page_count == 1
    assert result.open_order_page_count == 1
    assert result.closed_order_page_count == 2
    assert result.snapshot is not None
    assert len(result.orders) == 1
    assert result.fact_digest is not None
    assert len(result.fact_digest) == 32
    assert len(store.completed) == 1
    assert any("cursor=cursor-2" in request.path for request in raw.requests)
    assert len(raw.requests) == 14


@pytest.mark.asyncio
async def test_mysql_store_persists_sanitized_checkpoint_and_complete_bundle() -> None:
    repository = _Repository()
    store = MySqlTossUsReconciliationStore(
        cast(TossUsReconciliationRepository, repository)
    )
    raw = _RawTransport([*_capture_responses(), *_capture_responses()])

    result = await reconcile_toss_us_cash(
        BINDING_ID,
        as_of=AS_OF,
        context=_context(raw, store),
    )

    assert result.state == "COMPLETE"
    assert repository.completed is not None
    run, cash, positions, orders = repository.completed
    assert run.result == "COMPLETE"
    assert run.checkpoint is None
    assert run.fact_digest == result.fact_digest
    assert len(cash) == 1
    assert [position.symbol for position in positions] == ["AAPL"]
    assert [order.provider_order_id for order in orders] == ["open-order-1"]
    assert repository.row is not None
    assert repository.row.checkpoint is not None
    assert set(repository.row.checkpoint) == {
        "schemaVersion",
        "phase",
        "firstProjectionDigest",
    }


@pytest.mark.asyncio
async def test_process_restart_resumes_after_persisted_first_capture() -> None:
    store = _Store()
    first_raw = _RawTransport([*_capture_responses(), RuntimeError("outage")])

    first = await reconcile_toss_us_cash(
        BINDING_ID,
        as_of=AS_OF,
        context=_context(first_raw, store),
    )

    assert first.state == "PARTIAL"
    assert first.blockers == ("PROVIDER_UNAVAILABLE",)
    assert store.checkpoint is not None
    assert store.checkpoint.phase == "SECOND_CAPTURE"

    second_raw = _RawTransport(_capture_responses())
    second = await reconcile_toss_us_cash(
        BINDING_ID,
        as_of=AS_OF,
        context=_context(second_raw, store),
    )

    assert second.state == "COMPLETE"
    assert len(second_raw.requests) == 7
    assert store.checkpoint is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second_capture", "blocker"),
    [
        (_capture_responses(cash="3501"), "SNAPSHOT_DRIFT"),
        (_capture_responses(filled="2"), "SNAPSHOT_DRIFT"),
    ],
)
async def test_account_or_cumulative_fill_drift_never_completes(
    second_capture: list[BrokerResponse],
    blocker: str,
) -> None:
    raw = _RawTransport([*_capture_responses(), *second_capture])
    store = _Store()

    result = await reconcile_toss_us_cash(
        BINDING_ID,
        as_of=AS_OF,
        context=_context(raw, store),
    )

    assert result.state == "PARTIAL"
    assert result.blockers == (blocker,)
    assert store.completed == []
    assert store.checkpoint is not None
    assert store.checkpoint.phase == "FIRST_CAPTURE"


@pytest.mark.asyncio
async def test_partial_provider_outage_persists_nonpassing_checkpoint() -> None:
    raw = _RawTransport(
        [_capture_responses()[0], RuntimeError("private provider failure")]
    )
    store = _Store()

    result = await reconcile_toss_us_cash(
        BINDING_ID,
        as_of=AS_OF,
        context=_context(raw, store),
    )

    assert result.state == "PARTIAL"
    assert result.blockers == ("PROVIDER_UNAVAILABLE",)
    assert result.snapshot is None
    assert store.completed == []
    assert store.checkpoint is not None
    assert store.checkpoint.phase == "FIRST_CAPTURE"
    assert "private provider failure" not in repr(result)
