from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from autotrader.integrations.brokers.kis.cash_order_recovery import KisDailyOrder
from autotrader.shared.decimal import decimal_to_string, require_decimal
from autotrader.shared.ids import new_uuid7

_KST = ZoneInfo("Asia/Seoul")
_ACCOUNT_FACT_MAX_AGE = timedelta(seconds=30)
_PAGE_LIMIT = 10


class KisCashReconciliationStatus(StrEnum):
    COMPLETE = "COMPLETE"
    NON_PASSING = "NON_PASSING"


@dataclass(frozen=True, slots=True)
class KisCashHolding:
    symbol: str
    total_quantity: Decimal
    sellable_quantity: Decimal

    def __post_init__(self) -> None:
        _digits(self.symbol, 6, "holding symbol")
        total = _non_negative_integer(self.total_quantity, "total_quantity")
        sellable = _non_negative_integer(self.sellable_quantity, "sellable_quantity")
        if total <= 0 or sellable > total:
            raise ValueError("KIS cash holding quantities are invalid")
        object.__setattr__(self, "total_quantity", total)
        object.__setattr__(self, "sellable_quantity", sellable)


@dataclass(frozen=True, slots=True)
class KisAccountFacts:
    binding_id: UUID
    observed_at: datetime
    cash_buying_power: Decimal
    holdings: tuple[KisCashHolding, ...]
    source_digest: bytes

    def __post_init__(self) -> None:
        _uuid7(self.binding_id, "binding_id")
        _utc_second(self.observed_at, "observed_at")
        cash = _non_negative_integer(self.cash_buying_power, "cash_buying_power")
        if type(self.holdings) is not tuple or any(
            type(holding) is not KisCashHolding for holding in self.holdings
        ):
            raise TypeError("holdings must contain exact KisCashHolding values")
        holdings = self.holdings
        for holding in holdings:
            holding.__post_init__()
        symbols = tuple(holding.symbol for holding in holdings)
        if symbols != tuple(sorted(symbols)) or len(symbols) != len(set(symbols)):
            raise ValueError("KIS cash holdings must be unique and sorted")
        _digest(self.source_digest, "source_digest")
        object.__setattr__(self, "cash_buying_power", cash)


@dataclass(frozen=True, slots=True)
class KisDailyOrderPage:
    binding_id: UUID
    provider_trade_date: date
    orders: tuple[KisDailyOrder, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        _uuid7(self.binding_id, "binding_id")
        if type(self.provider_trade_date) is not date:
            raise TypeError("provider_trade_date must be an exact date")
        if type(self.orders) is not tuple or any(
            type(order) is not KisDailyOrder for order in self.orders
        ):
            raise TypeError("orders must contain exact KisDailyOrder values")
        for order in self.orders:
            order.__post_init__()
        if self.next_cursor is not None and (
            type(self.next_cursor) is not str
            or not self.next_cursor
            or len(self.next_cursor) > 256
            or "\n" in self.next_cursor
            or "\r" in self.next_cursor
        ):
            raise ValueError("KIS daily order continuation cursor is invalid")


@dataclass(frozen=True, slots=True)
class KisCashReconciliationResult:
    reconciliation_id: UUID
    binding_id: UUID
    as_of: datetime
    provider_trade_date: date
    status: KisCashReconciliationStatus
    blockers: tuple[str, ...]
    page_count: int
    orders: tuple[KisDailyOrder, ...]
    cash_buying_power: Decimal
    holdings: tuple[KisCashHolding, ...]
    account_source_digest: bytes | None
    account_observed_at: datetime | None
    result_digest: bytes

    def __post_init__(self) -> None:
        _uuid7(self.reconciliation_id, "reconciliation_id")
        _uuid7(self.binding_id, "binding_id")
        _utc_second(self.as_of, "as_of")
        if type(self.provider_trade_date) is not date:
            raise TypeError("provider_trade_date must be an exact date")
        if type(self.status) is not KisCashReconciliationStatus:
            raise TypeError("reconciliation status must be exact")
        if type(self.blockers) is not tuple or any(
            type(blocker) is not str or not blocker for blocker in self.blockers
        ):
            raise TypeError("blockers must be exact stable codes")
        if self.status is KisCashReconciliationStatus.COMPLETE:
            if (
                self.blockers
                or self.account_source_digest is None
                or self.account_observed_at is None
                or any(
                    order.cumulative_filled_quantity > 0 and order.fee_amount is None
                    for order in self.orders
                )
            ):
                raise ValueError("complete KIS reconciliation has blockers")
        elif not self.blockers:
            raise ValueError("non-passing KIS reconciliation requires blockers")
        if type(self.page_count) is not int or not 0 <= self.page_count <= _PAGE_LIMIT:
            raise ValueError("page_count is invalid")
        if type(self.orders) is not tuple or any(
            type(order) is not KisDailyOrder for order in self.orders
        ):
            raise TypeError("orders must contain exact values")
        if type(self.holdings) is not tuple or any(
            type(holding) is not KisCashHolding for holding in self.holdings
        ):
            raise TypeError("holdings must contain exact values")
        _non_negative_integer(self.cash_buying_power, "cash_buying_power")
        if self.account_source_digest is not None:
            _digest(self.account_source_digest, "account_source_digest")
        if self.account_observed_at is not None:
            _utc_second(self.account_observed_at, "account_observed_at")
        if (self.account_source_digest is None) != (self.account_observed_at is None):
            raise ValueError("account fact time and digest must be present together")
        _digest(self.result_digest, "result_digest")
        if self.result_digest != _result_digest(
            binding_id=self.binding_id,
            as_of=self.as_of,
            provider_trade_date=self.provider_trade_date,
            status=self.status,
            blockers=self.blockers,
            page_count=self.page_count,
            orders=self.orders,
            cash_buying_power=self.cash_buying_power,
            holdings=self.holdings,
            account_source_digest=self.account_source_digest,
            account_observed_at=self.account_observed_at,
        ):
            raise ValueError("KIS reconciliation result digest is invalid")

    @property
    def open_order_count(self) -> int:
        return sum(order.remaining_quantity > 0 for order in self.orders)

    @property
    def cumulative_fee_amount(self) -> Decimal:
        return sum(
            (order.fee_amount for order in self.orders if order.fee_amount is not None),
            Decimal(0),
        )


class KisCashReconciliationSource(Protocol):
    async def read_daily_order_page(
        self,
        binding_id: UUID,
        trade_date: date,
        cursor: str | None,
    ) -> KisDailyOrderPage: ...

    async def read_account_facts(
        self,
        binding_id: UUID,
        as_of: datetime,
    ) -> KisAccountFacts: ...


class KisCashReconciliationStore(Protocol):
    async def persist_complete(self, result: KisCashReconciliationResult) -> None: ...


async def reconcile_kis_cash(
    binding_id: UUID,
    as_of: datetime,
    *,
    source: KisCashReconciliationSource,
    store: KisCashReconciliationStore,
) -> KisCashReconciliationResult:
    _uuid7(binding_id, "binding_id")
    _utc_second(as_of, "as_of")
    provider_trade_date = as_of.astimezone(_KST).date()
    reconciliation_id = new_uuid7()
    blockers: list[str] = []
    first_orders, page_count, first_blocker = await _collect_order_pass(
        binding_id=binding_id,
        provider_trade_date=provider_trade_date,
        source=source,
    )
    if first_blocker is not None:
        blockers.append(first_blocker)

    account: KisAccountFacts | None = None
    if not blockers:
        try:
            account = await source.read_account_facts(binding_id, as_of)
        except Exception:
            blockers.append("ACCOUNT_FACT_FAILURE")
        else:
            if account.binding_id != binding_id:
                blockers.append("ACCOUNT_FACT_SCOPE_MISMATCH")
            if account.observed_at > as_of:
                blockers.append("ACCOUNT_FACT_TIME_INVALID")
            elif as_of - account.observed_at > _ACCOUNT_FACT_MAX_AGE:
                blockers.append("ACCOUNT_FACT_STALE")

    if not blockers:
        second_orders, second_page_count, second_blocker = await _collect_order_pass(
            binding_id=binding_id,
            provider_trade_date=provider_trade_date,
            source=source,
        )
        if second_blocker is not None:
            blockers.append(second_blocker)
        elif second_page_count != page_count or second_orders != first_orders:
            blockers.append("UNSTABLE_ORDER_SNAPSHOT")

    if not blockers and any(
        order.cumulative_filled_quantity > 0 and order.fee_amount is None
        for order in first_orders
    ):
        blockers.append("FILL_FEE_EVIDENCE_MISSING")

    result = _build_result(
        reconciliation_id=reconciliation_id,
        binding_id=binding_id,
        as_of=as_of,
        provider_trade_date=provider_trade_date,
        blockers=tuple(blockers),
        page_count=page_count,
        orders=first_orders,
        account=account,
    )
    if result.status is KisCashReconciliationStatus.COMPLETE:
        await store.persist_complete(result)
    return result


async def _collect_order_pass(
    *,
    binding_id: UUID,
    provider_trade_date: date,
    source: KisCashReconciliationSource,
) -> tuple[tuple[KisDailyOrder, ...], int, str | None]:
    orders: list[KisDailyOrder] = []
    seen_cursors: set[str] = set()
    cursor: str | None = None
    page_count = 0
    for _ in range(_PAGE_LIMIT):
        try:
            page = await source.read_daily_order_page(
                binding_id, provider_trade_date, cursor
            )
        except Exception:
            return tuple(orders), page_count, "PROVIDER_PAGE_FAILURE"
        page_count += 1
        if page.binding_id != binding_id:
            return tuple(orders), page_count, "ACCOUNT_SCOPE_MISMATCH"
        if page.provider_trade_date != provider_trade_date:
            return tuple(orders), page_count, "PROVIDER_DAY_MISMATCH"
        if any(order.binding_id != binding_id for order in page.orders):
            return tuple(orders), page_count, "ORDER_ACCOUNT_SCOPE_MISMATCH"
        orders.extend(page.orders)
        if page.next_cursor is None:
            identities = tuple(order.provider_identity for order in orders)
            if len(identities) != len(set(identities)):
                return (
                    tuple(orders),
                    page_count,
                    "DUPLICATE_PROVIDER_ORDER_IDENTITY",
                )
            return tuple(orders), page_count, None
        if page.next_cursor in seen_cursors:
            return tuple(orders), page_count, "REPEATED_PROVIDER_CURSOR"
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor
    return tuple(orders), page_count, "PROVIDER_PAGE_LIMIT_EXHAUSTED"


def _build_result(
    *,
    reconciliation_id: UUID,
    binding_id: UUID,
    as_of: datetime,
    provider_trade_date: date,
    blockers: tuple[str, ...],
    page_count: int,
    orders: tuple[KisDailyOrder, ...],
    account: KisAccountFacts | None,
) -> KisCashReconciliationResult:
    status = (
        KisCashReconciliationStatus.NON_PASSING
        if blockers
        else KisCashReconciliationStatus.COMPLETE
    )
    cash = Decimal(0) if account is None else account.cash_buying_power
    holdings = () if account is None else account.holdings
    account_digest = None if account is None else account.source_digest
    account_observed_at = None if account is None else account.observed_at
    digest = _result_digest(
        binding_id=binding_id,
        as_of=as_of,
        provider_trade_date=provider_trade_date,
        status=status,
        blockers=blockers,
        page_count=page_count,
        orders=orders,
        cash_buying_power=cash,
        holdings=holdings,
        account_source_digest=account_digest,
        account_observed_at=account_observed_at,
    )
    return KisCashReconciliationResult(
        reconciliation_id=reconciliation_id,
        binding_id=binding_id,
        as_of=as_of,
        provider_trade_date=provider_trade_date,
        status=status,
        blockers=blockers,
        page_count=page_count,
        orders=orders,
        cash_buying_power=cash,
        holdings=holdings,
        account_source_digest=account_digest,
        account_observed_at=account_observed_at,
        result_digest=digest,
    )


def _result_digest(
    *,
    binding_id: UUID,
    as_of: datetime,
    provider_trade_date: date,
    status: KisCashReconciliationStatus,
    blockers: tuple[str, ...],
    page_count: int,
    orders: tuple[KisDailyOrder, ...],
    cash_buying_power: Decimal,
    holdings: tuple[KisCashHolding, ...],
    account_source_digest: bytes | None,
    account_observed_at: datetime | None,
) -> bytes:
    payload = {
        "accountSourceDigest": (
            None if account_source_digest is None else account_source_digest.hex()
        ),
        "accountObservedAt": (
            None if account_observed_at is None else account_observed_at.isoformat()
        ),
        "asOf": as_of.isoformat(),
        "bindingId": binding_id.hex,
        "blockers": blockers,
        "cashBuyingPower": decimal_to_string(cash_buying_power),
        "holdings": [
            {
                "sellableQuantity": decimal_to_string(holding.sellable_quantity),
                "symbol": holding.symbol,
                "totalQuantity": decimal_to_string(holding.total_quantity),
            }
            for holding in holdings
        ],
        "orderDigests": [order.record_digest.hex() for order in orders],
        "pageCount": page_count,
        "providerTradeDate": provider_trade_date.isoformat(),
        "status": status.value,
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


def _non_negative_integer(value: object, name: str) -> Decimal:
    try:
        result = require_decimal(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite Decimal") from error
    if result < 0 or result != result.to_integral_value():
        raise ValueError(f"{name} must be a non-negative integer")
    return result


def _uuid7(value: object, name: str) -> UUID:
    if type(value) is not UUID or value.version != 7:
        raise ValueError(f"{name} must be UUIDv7")
    return value


def _utc_second(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
        or value.microsecond != 0
    ):
        raise ValueError(f"{name} must be exact whole-second UTC")
    return value


def _digits(value: object, width: int, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != width
        or not value.isascii()
        or not value.isdecimal()
    ):
        raise ValueError(f"{name} must be {width} ASCII digits")
    return value


def _digest(value: object, name: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        raise ValueError(f"{name} must be a 32-byte SHA-256 digest")
    return value


__all__ = (
    "KisAccountFacts",
    "KisCashHolding",
    "KisCashReconciliationResult",
    "KisCashReconciliationSource",
    "KisCashReconciliationStatus",
    "KisCashReconciliationStore",
    "KisDailyOrderPage",
    "reconcile_kis_cash",
)
