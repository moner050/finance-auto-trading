from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Protocol, cast
from urllib.parse import urlencode
from uuid import UUID
from zoneinfo import ZoneInfo

from autotrader.integrations.brokers.common import (
    AsyncHttpTransport,
    BrokerRequest,
    BrokerResponse,
)
from autotrader.integrations.brokers.toss.account_snapshot import (
    decode_toss_holdings,
)
from autotrader.integrations.brokers.toss.us_account_snapshot import (
    TossUsAccountSnapshot,
    TossUsSnapshotCapture,
    capture_toss_us_snapshot,
)
from autotrader.integrations.brokers.toss.us_orders import (
    ProviderTimeWindow,
    TossUsOrderCapture,
    TossUsOrderFact,
    TossUsOrderPage,
    read_toss_us_orders,
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
from autotrader.shared.decimal import decimal_to_string, parse_contract_decimal
from autotrader.shared.ids import new_uuid7

_KST = ZoneInfo("Asia/Seoul")
_MAX_CAPTURE_SECONDS = 30
_MAX_CLOSED_PAGES = 100


class TossUsReconciliationUnavailable(RuntimeError):
    """Raised when reconciliation safety inputs or checkpoint writes are invalid."""


@dataclass(frozen=True, slots=True)
class TossUsReconciliationCheckpoint:
    run_id: UUID
    binding_id: UUID
    account_id: UUID
    provider_as_of: datetime
    started_at: datetime
    updated_at: datetime
    phase: str
    first_projection_digest: bytes | None

    def __post_init__(self) -> None:
        for name in ("run_id", "binding_id", "account_id"):
            if type(getattr(self, name)) is not UUID:
                raise TypeError(f"{name} must be UUID")
        provider_as_of = _utc(self.provider_as_of, "provider_as_of")
        started_at = _utc(self.started_at, "started_at")
        updated_at = _utc(self.updated_at, "updated_at")
        if started_at < provider_as_of or updated_at < started_at:
            raise ValueError("checkpoint starts before provider as-of")
        if self.phase not in {"FIRST_CAPTURE", "SECOND_CAPTURE"}:
            raise ValueError("checkpoint phase is invalid")
        if self.phase == "FIRST_CAPTURE":
            if self.first_projection_digest is not None:
                raise ValueError("first-capture checkpoint contains a projection")
        else:
            _digest(self.first_projection_digest, "first projection")
        object.__setattr__(self, "provider_as_of", provider_as_of)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "updated_at", updated_at)


@dataclass(frozen=True, slots=True)
class TossUsReconciliationResult:
    run_id: UUID
    binding_id: UUID
    account_id: UUID
    provider_as_of: datetime
    started_at: datetime
    completed_at: datetime
    state: str
    holdings_page_count: int
    open_order_page_count: int
    closed_order_page_count: int
    snapshot: TossUsAccountSnapshot | None
    orders: tuple[TossUsOrderFact, ...]
    commission_digest: bytes | None
    fact_digest: bytes | None
    blockers: tuple[str, ...]


class TossUsReconciliationStore(Protocol):
    def load_checkpoint(
        self,
        binding_id: UUID,
        provider_as_of: datetime,
    ) -> Coroutine[object, object, TossUsReconciliationCheckpoint | None]: ...

    def save_checkpoint(
        self,
        checkpoint: TossUsReconciliationCheckpoint,
    ) -> Coroutine[object, object, None]: ...

    def persist_complete(
        self,
        result: TossUsReconciliationResult,
    ) -> Coroutine[object, object, None]: ...


@dataclass(frozen=True, slots=True)
class TossUsReconciliationContext:
    account_id: UUID
    account_scope_digest: bytes = field(repr=False)
    access_token: str = field(repr=False)
    account_header: str = field(repr=False)
    transport: AsyncHttpTransport = field(repr=False)
    store: TossUsReconciliationStore = field(repr=False)
    clock: Callable[[], datetime] = field(repr=False)
    new_run_id: Callable[[], UUID] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.account_id) is not UUID:
            raise TypeError("account_id must be UUID")
        _digest(self.account_scope_digest, "account scope")
        for value, name in (
            (self.access_token, "access_token"),
            (self.account_header, "account_header"),
        ):
            if type(value) is not str or not value or "\n" in value:
                raise ValueError(f"{name} is invalid")
        if not callable(self.clock) or not callable(self.new_run_id):
            raise TypeError("reconciliation callbacks are invalid")


@dataclass(frozen=True, slots=True)
class MySqlTossUsReconciliationStore:
    repository: TossUsReconciliationRepository = field(repr=False)

    async def load_checkpoint(
        self,
        binding_id: UUID,
        provider_as_of: datetime,
    ) -> TossUsReconciliationCheckpoint | None:
        row = await self.repository.load_checkpoint(
            binding_id=binding_id,
            provider_as_of=provider_as_of,
        )
        if row is None:
            return None
        payload = row.checkpoint
        if type(payload) is not dict or set(payload) != {
            "schemaVersion",
            "phase",
            "firstProjectionDigest",
        }:
            raise ValueError("persisted Toss US checkpoint is invalid")
        raw_digest = payload["firstProjectionDigest"]
        if raw_digest is None:
            digest = None
        elif type(raw_digest) is str:
            try:
                digest = bytes.fromhex(raw_digest)
            except ValueError as error:
                raise ValueError("persisted Toss US checkpoint is invalid") from error
        else:
            raise ValueError("persisted Toss US checkpoint is invalid")
        return TossUsReconciliationCheckpoint(
            run_id=row.id,
            binding_id=row.binding_id,
            account_id=row.account_id,
            provider_as_of=row.provider_as_of,
            started_at=row.started_at,
            updated_at=row.updated_at,
            phase=cast(str, payload["phase"]),
            first_projection_digest=digest,
        )

    async def save_checkpoint(
        self,
        checkpoint: TossUsReconciliationCheckpoint,
    ) -> None:
        await self.repository.persist_checkpoint(
            TossUsReconciliationRunRow(
                id=checkpoint.run_id,
                binding_id=checkpoint.binding_id,
                account_id=checkpoint.account_id,
                provider_code="TOSS",
                market_country="US",
                settlement_asset="USD",
                provider_as_of=checkpoint.provider_as_of,
                started_at=checkpoint.started_at,
                updated_at=checkpoint.updated_at,
                completed_at=None,
                result="IN_PROGRESS",
                holdings_page_count=0,
                open_order_page_count=0,
                closed_order_page_count=0,
                missing_page_count=0,
                cash_fact_count=0,
                position_fact_count=0,
                order_fact_count=0,
                fact_digest=None,
                blockers=[],
                checkpoint={
                    "schemaVersion": 1,
                    "phase": checkpoint.phase,
                    "firstProjectionDigest": (
                        None
                        if checkpoint.first_projection_digest is None
                        else checkpoint.first_projection_digest.hex()
                    ),
                },
            )
        )

    async def persist_complete(self, result: TossUsReconciliationResult) -> None:
        if result.state != "COMPLETE" or result.snapshot is None:
            raise ValueError("only complete Toss US reconciliation can persist")
        fact_digest = _digest(result.fact_digest, "fact digest")
        run = TossUsReconciliationRunRow(
            id=result.run_id,
            binding_id=result.binding_id,
            account_id=result.account_id,
            provider_code="TOSS",
            market_country="US",
            settlement_asset="USD",
            provider_as_of=result.provider_as_of,
            started_at=result.started_at,
            updated_at=result.completed_at,
            completed_at=result.completed_at,
            result="COMPLETE",
            holdings_page_count=result.holdings_page_count,
            open_order_page_count=result.open_order_page_count,
            closed_order_page_count=result.closed_order_page_count,
            missing_page_count=0,
            cash_fact_count=1,
            position_fact_count=len(result.snapshot.positions),
            order_fact_count=len(result.orders),
            fact_digest=fact_digest,
            blockers=[],
            checkpoint=None,
        )
        cash = result.snapshot.cash_fact
        await self.repository.persist_completed_run(
            run=run,
            cash_facts=(
                TossUsCashFactRow(
                    id=new_uuid7(),
                    run_id=result.run_id,
                    settlement_asset="USD",
                    state=cash.state,
                    available_cash=cash.available_cash,
                    settled_cash=cash.settled_cash,
                    source_field=cash.source_field,
                    provider_as_of=cash.provider_as_of,
                    captured_at=cash.captured_at,
                    source_digest=cash.source_digest,
                ),
            ),
            position_facts=tuple(
                TossUsPositionFactRow(
                    id=new_uuid7(),
                    run_id=result.run_id,
                    symbol=position.symbol,
                    settlement_asset="USD",
                    total_quantity=position.total_quantity,
                    sellable_quantity=position.sellable_quantity,
                    average_price=position.average_price,
                    market_value=position.market_value,
                    provider_as_of=position.provider_as_of,
                    captured_at=position.captured_at,
                    source_digest=position.source_digest,
                )
                for position in result.snapshot.positions
            ),
            order_facts=tuple(
                TossUsOrderFactRow(
                    id=new_uuid7(),
                    run_id=result.run_id,
                    provider_order_id=order.provider_order_id,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=order.quantity,
                    cumulative_fill_quantity=order.cumulative_fill_quantity,
                    state=order.state,
                    limit_price=order.limit_price,
                    commission=order.commission,
                    tax=order.tax,
                    settlement_asset="USD",
                    ordered_at=order.ordered_at,
                    filled_at=order.filled_at,
                    canceled_at=order.canceled_at,
                    provider_as_of=order.provider_as_of,
                    captured_at=order.captured_at,
                    source_digest=order.source_digest,
                )
                for order in result.orders
            ),
        )


@dataclass(frozen=True, slots=True)
class _Capture:
    snapshot: TossUsAccountSnapshot
    orders: tuple[TossUsOrderFact, ...]
    commission_rate: Decimal
    commission_digest: bytes
    projection_digest: bytes
    fact_digest: bytes
    open_order_page_count: int
    closed_order_page_count: int


async def reconcile_toss_us_cash(
    binding_id: UUID,
    *,
    as_of: datetime,
    context: TossUsReconciliationContext,
) -> TossUsReconciliationResult:
    if type(binding_id) is not UUID or type(context) is not TossUsReconciliationContext:
        raise TossUsReconciliationUnavailable("Toss US reconciliation input is invalid")
    provider_as_of = _utc(as_of, "as_of")
    try:
        checkpoint = await context.store.load_checkpoint(binding_id, provider_as_of)
        if checkpoint is None:
            checkpoint = TossUsReconciliationCheckpoint(
                run_id=context.new_run_id(),
                binding_id=binding_id,
                account_id=context.account_id,
                provider_as_of=provider_as_of,
                started_at=provider_as_of,
                updated_at=_capture_time(provider_as_of, context.clock),
                phase="FIRST_CAPTURE",
                first_projection_digest=None,
            )
            await context.store.save_checkpoint(checkpoint)
        _require_checkpoint(checkpoint, binding_id=binding_id, context=context)

        if checkpoint.phase == "FIRST_CAPTURE":
            try:
                first = await _capture_once(provider_as_of, context=context)
            except Exception:
                await context.store.save_checkpoint(checkpoint)
                return _partial(
                    checkpoint,
                    completed_at=_capture_time(provider_as_of, context.clock),
                    blocker="PROVIDER_UNAVAILABLE",
                )
            checkpoint = TossUsReconciliationCheckpoint(
                run_id=checkpoint.run_id,
                binding_id=checkpoint.binding_id,
                account_id=checkpoint.account_id,
                provider_as_of=checkpoint.provider_as_of,
                started_at=checkpoint.started_at,
                updated_at=_capture_time(provider_as_of, context.clock),
                phase="SECOND_CAPTURE",
                first_projection_digest=first.projection_digest,
            )
            await context.store.save_checkpoint(checkpoint)

        try:
            second = await _capture_once(provider_as_of, context=context)
        except Exception:
            await context.store.save_checkpoint(checkpoint)
            return _partial(
                checkpoint,
                completed_at=_capture_time(provider_as_of, context.clock),
                blocker="PROVIDER_UNAVAILABLE",
            )
        if checkpoint.first_projection_digest != second.projection_digest:
            reset = TossUsReconciliationCheckpoint(
                run_id=checkpoint.run_id,
                binding_id=checkpoint.binding_id,
                account_id=checkpoint.account_id,
                provider_as_of=checkpoint.provider_as_of,
                started_at=checkpoint.started_at,
                updated_at=_capture_time(provider_as_of, context.clock),
                phase="FIRST_CAPTURE",
                first_projection_digest=None,
            )
            await context.store.save_checkpoint(reset)
            return _partial(
                reset,
                completed_at=_capture_time(provider_as_of, context.clock),
                blocker="SNAPSHOT_DRIFT",
                capture=second,
            )

        result = TossUsReconciliationResult(
            run_id=checkpoint.run_id,
            binding_id=binding_id,
            account_id=context.account_id,
            provider_as_of=provider_as_of,
            started_at=checkpoint.started_at,
            completed_at=_capture_time(provider_as_of, context.clock),
            state="COMPLETE",
            holdings_page_count=second.snapshot.holdings_page_count,
            open_order_page_count=second.open_order_page_count,
            closed_order_page_count=second.closed_order_page_count,
            snapshot=second.snapshot,
            orders=second.orders,
            commission_digest=second.commission_digest,
            fact_digest=second.fact_digest,
            blockers=(),
        )
        await context.store.persist_complete(result)
        return result
    except TossUsReconciliationUnavailable:
        raise
    except Exception:
        raise TossUsReconciliationUnavailable(
            "Toss US reconciliation checkpoint is unavailable"
        ) from None


async def _capture_once(
    provider_as_of: datetime,
    *,
    context: TossUsReconciliationContext,
) -> _Capture:
    headers = (
        ("Authorization", f"Bearer {context.access_token}"),
        ("X-Tossinvest-Account", context.account_header),
    )
    buying_power = await _get(
        context.transport,
        "/api/v1/buying-power?currency=USD",
        headers,
    )
    holdings = await _get(context.transport, "/api/v1/holdings", headers)
    decoded_holdings = decode_toss_holdings(holdings)
    symbols = tuple(
        sorted(
            holding.symbol
            for holding in decoded_holdings
            if holding.market_country == "US"
        )
    )
    sellable: dict[str, BrokerResponse] = {}
    for symbol in symbols:
        sellable[symbol] = await _get(
            context.transport,
            f"/api/v1/sellable-quantity?{urlencode((('symbol', symbol),))}",
            headers,
        )

    window = ProviderTimeWindow(
        started_at=provider_as_of - timedelta(days=30),
        ended_at=provider_as_of,
    )
    start_date = window.started_at.astimezone(_KST).date().isoformat()
    end_date = window.ended_at.astimezone(_KST).date().isoformat()
    open_page = TossUsOrderPage(
        requested_cursor=None,
        response=await _get(
            context.transport,
            "/api/v1/orders?"
            + urlencode((("status", "OPEN"), ("from", start_date), ("to", end_date))),
            headers,
        ),
    )
    closed_pages = await _closed_pages(
        context.transport,
        headers=headers,
        start_date=start_date,
        end_date=end_date,
    )
    commissions = await _get(context.transport, "/api/v1/commissions", headers)
    captured_at = _capture_time(provider_as_of, context.clock)
    snapshot = await capture_toss_us_snapshot(
        provider_as_of,
        capture=TossUsSnapshotCapture(
            account_scope_digest=context.account_scope_digest,
            buying_power_response=buying_power,
            holdings_response=holdings,
            sellable_responses=sellable,
        ),
        captured_at=captured_at,
    )
    orders = await read_toss_us_orders(
        window,
        captures=(
            TossUsOrderCapture(
                status="OPEN",
                account_scope_digest=context.account_scope_digest,
                pages=(open_page,),
            ),
            TossUsOrderCapture(
                status="CLOSED",
                account_scope_digest=context.account_scope_digest,
                pages=closed_pages,
            ),
        ),
        captured_at=captured_at,
    )
    commission_rate, commission_digest = _decode_us_commission(commissions)
    projection_digest = _projection_digest(snapshot, orders, commission_rate)
    return _Capture(
        snapshot=snapshot,
        orders=orders,
        commission_rate=commission_rate,
        commission_digest=commission_digest,
        projection_digest=projection_digest,
        fact_digest=_fact_digest(
            snapshot,
            orders,
            commission_digest,
            projection_digest,
        ),
        open_order_page_count=1,
        closed_order_page_count=len(closed_pages),
    )


async def _closed_pages(
    transport: AsyncHttpTransport,
    *,
    headers: tuple[tuple[str, str], ...],
    start_date: str,
    end_date: str,
) -> tuple[TossUsOrderPage, ...]:
    pages: list[TossUsOrderPage] = []
    cursor: str | None = None
    while True:
        query = [
            ("status", "CLOSED"),
            ("from", start_date),
            ("to", end_date),
            ("limit", "100"),
        ]
        if cursor is not None:
            query.append(("cursor", cursor))
        response = await _get(
            transport,
            f"/api/v1/orders?{urlencode(query)}",
            headers,
        )
        page = TossUsOrderPage(requested_cursor=cursor, response=response)
        pages.append(page)
        next_cursor = _next_cursor(response)
        if next_cursor is None:
            return tuple(pages)
        if len(pages) >= _MAX_CLOSED_PAGES:
            raise ValueError("Toss CLOSED order pagination is unbounded")
        cursor = next_cursor


async def _get(
    transport: AsyncHttpTransport,
    path: str,
    headers: tuple[tuple[str, str], ...],
) -> BrokerResponse:
    return await transport.request(
        BrokerRequest(method="GET", path=path, headers=headers)
    )


def _next_cursor(response: BrokerResponse) -> str | None:
    result = _result(response)
    if set(result) != {"orders", "nextCursor", "hasNext"}:
        raise ValueError("Toss order page shape is invalid")
    has_next = result.get("hasNext")
    cursor = result.get("nextCursor")
    if type(has_next) is not bool:
        raise ValueError("Toss order page continuation is invalid")
    if has_next:
        if type(cursor) is not str or not cursor or "\n" in cursor:
            raise ValueError("Toss order page cursor is invalid")
        return cursor
    if cursor is not None:
        raise ValueError("Toss terminal order page has a cursor")
    return None


def _decode_us_commission(response: BrokerResponse) -> tuple[Decimal, bytes]:
    result = _result_value(response)
    if type(result) is not list:
        raise ValueError("Toss commission response is invalid")
    rates: list[Decimal] = []
    for raw in cast(list[object], result):
        item = _mapping(raw, "commission")
        if not set(item) >= {"marketCountry", "commissionRate"}:
            raise ValueError("Toss commission item is invalid")
        country = item.get("marketCountry")
        if country not in {"KR", "US"}:
            raise ValueError("Toss commission country is invalid")
        rate = _decimal(item.get("commissionRate"), "commissionRate")
        if rate < 0:
            raise ValueError("Toss commission rate is invalid")
        for name in ("startDate", "endDate"):
            _optional_date(item.get(name))
        if country == "US":
            rates.append(rate)
    if len(rates) != 1:
        raise ValueError("one direct US commission rate is required")
    return rates[0], hashlib.sha256(response.body).digest()


def _projection_digest(
    snapshot: TossUsAccountSnapshot,
    orders: tuple[TossUsOrderFact, ...],
    commission_rate: Decimal,
) -> bytes:
    payload = {
        "cash": decimal_to_string(snapshot.cash_fact.available_cash),
        "commissionRate": decimal_to_string(commission_rate),
        "orders": [
            {
                "commission": _decimal_text(order.commission),
                "filled": decimal_to_string(order.cumulative_fill_quantity),
                "id": order.provider_order_id,
                "quantity": decimal_to_string(order.quantity),
                "state": order.state,
                "tax": _decimal_text(order.tax),
            }
            for order in orders
        ],
        "positions": [
            {
                "averagePrice": decimal_to_string(position.average_price),
                "marketValue": decimal_to_string(position.market_value),
                "sellable": decimal_to_string(position.sellable_quantity),
                "symbol": position.symbol,
                "total": decimal_to_string(position.total_quantity),
            }
            for position in snapshot.positions
        ],
        "settlementAsset": "USD",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


def _fact_digest(
    snapshot: TossUsAccountSnapshot,
    orders: tuple[TossUsOrderFact, ...],
    commission_digest: bytes,
    projection_digest: bytes,
) -> bytes:
    digest = hashlib.sha256()
    values = (
        b"TOSS_US_RECONCILIATION_FACT_V1",
        snapshot.source_digest,
        *(order.source_digest for order in orders),
        commission_digest,
        projection_digest,
    )
    for value in values:
        digest.update(len(value).to_bytes(4, "big"))
        digest.update(value)
    return digest.digest()


def _partial(
    checkpoint: TossUsReconciliationCheckpoint,
    *,
    completed_at: datetime,
    blocker: str,
    capture: _Capture | None = None,
) -> TossUsReconciliationResult:
    return TossUsReconciliationResult(
        run_id=checkpoint.run_id,
        binding_id=checkpoint.binding_id,
        account_id=checkpoint.account_id,
        provider_as_of=checkpoint.provider_as_of,
        started_at=checkpoint.started_at,
        completed_at=completed_at,
        state="PARTIAL",
        holdings_page_count=(
            0 if capture is None else capture.snapshot.holdings_page_count
        ),
        open_order_page_count=(0 if capture is None else capture.open_order_page_count),
        closed_order_page_count=(
            0 if capture is None else capture.closed_order_page_count
        ),
        snapshot=None if capture is None else capture.snapshot,
        orders=() if capture is None else capture.orders,
        commission_digest=None if capture is None else capture.commission_digest,
        fact_digest=None if capture is None else capture.fact_digest,
        blockers=(blocker,),
    )


def _require_checkpoint(
    checkpoint: TossUsReconciliationCheckpoint,
    *,
    binding_id: UUID,
    context: TossUsReconciliationContext,
) -> None:
    if (
        type(checkpoint) is not TossUsReconciliationCheckpoint
        or checkpoint.binding_id != binding_id
        or checkpoint.account_id != context.account_id
    ):
        raise TossUsReconciliationUnavailable(
            "Toss US reconciliation checkpoint scope is invalid"
        )


def _capture_time(
    provider_as_of: datetime,
    clock: Callable[[], datetime],
) -> datetime:
    captured_at = _utc(clock(), "clock")
    if (
        not provider_as_of
        <= captured_at
        <= provider_as_of + timedelta(seconds=_MAX_CAPTURE_SECONDS)
    ):
        raise ValueError("Toss US reconciliation capture time is unbounded")
    return captured_at


def _result(response: BrokerResponse) -> Mapping[str, object]:
    return _mapping(_result_value(response), "provider result")


def _result_value(response: BrokerResponse) -> object:
    if type(response) is not BrokerResponse or response.status != 200:
        raise ValueError("provider response is invalid")
    try:
        payload: object = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("provider response is invalid") from error
    envelope = _mapping(payload, "provider envelope")
    if set(envelope) != {"result"}:
        raise ValueError("provider envelope is invalid")
    return envelope["result"]


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    raw = cast(Mapping[object, object], value)
    if any(type(key) is not str for key in raw):
        raise TypeError(f"{name} must have string keys")
    return cast(Mapping[str, object], raw)


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is not str:
        raise TypeError(f"{name} must be decimal text")
    return parse_contract_decimal(value)


def _optional_date(value: object) -> None:
    if value is None:
        return
    if type(value) is not str:
        raise TypeError("commission date is invalid")
    date.fromisoformat(value)


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else decimal_to_string(value)


def _utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _digest(value: object, name: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        raise TypeError(f"{name} must be SHA-256 bytes")
    return value
