from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.integrations.brokers.kis.cash_order_recovery import KisDailyOrder
from autotrader.integrations.brokers.kis.cash_reconciliation import (
    KisAccountFacts,
    KisCashHolding,
    KisCashReconciliationResult,
    KisCashReconciliationStatus,
    KisDailyOrderPage,
    reconcile_kis_cash,
)
from autotrader.shared.ids import new_uuid7

AS_OF = datetime(2026, 8, 24, 0, 20, 0, tzinfo=UTC)


def _order(binding_id: UUID, **changes: object) -> KisDailyOrder:
    values: dict[str, object] = {
        "binding_id": binding_id,
        "order_date": "20260824",
        "organization_number": "12345",
        "order_number": "0000000042",
        "original_order_number": "0000000000",
        "provider_timestamp": AS_OF - timedelta(seconds=10),
        "side": Side.BUY,
        "symbol": "005930",
        "order_style": OrderStyle.LIMIT,
        "order_quantity": Decimal("10"),
        "limit_price": Decimal("70000"),
        "cumulative_filled_quantity": Decimal("4"),
        "average_fill_price": Decimal("69900"),
        "total_filled_amount": Decimal("279600"),
        "confirmed_cancelled_quantity": Decimal("0"),
        "remaining_quantity": Decimal("6"),
        "rejected_quantity": Decimal("0"),
        "fee_amount": Decimal("28"),
    }
    values.update(changes)
    return KisDailyOrder(**values)  # type: ignore[arg-type]


def _account(binding_id: UUID, **changes: object) -> KisAccountFacts:
    values: dict[str, object] = {
        "binding_id": binding_id,
        "observed_at": AS_OF - timedelta(seconds=1),
        "cash_buying_power": Decimal("5000000"),
        "holdings": (
            KisCashHolding(
                symbol="005930",
                total_quantity=Decimal("10"),
                sellable_quantity=Decimal("6"),
            ),
        ),
        "source_digest": b"a" * 32,
    }
    values.update(changes)
    return KisAccountFacts(**values)  # type: ignore[arg-type]


class Source:
    def __init__(
        self,
        pages: dict[str | None, KisDailyOrderPage | Exception],
        account: KisAccountFacts,
    ) -> None:
        self.pages = pages
        self.account = account
        self.cursors: list[str | None] = []

    async def read_daily_order_page(
        self, binding_id: UUID, trade_date: date, cursor: str | None
    ) -> KisDailyOrderPage:
        del binding_id, trade_date
        self.cursors.append(cursor)
        value = self.pages[cursor]
        if isinstance(value, Exception):
            raise value
        return value

    async def read_account_facts(
        self, binding_id: UUID, as_of: datetime
    ) -> KisAccountFacts:
        del binding_id, as_of
        return self.account


class Store:
    def __init__(self) -> None:
        self.persisted: list[KisCashReconciliationResult] = []

    async def persist_complete(self, result: KisCashReconciliationResult) -> None:
        self.persisted.append(result)


@pytest.mark.asyncio
async def test_complete_reconciliation_paginates_and_persists_all_account_facts() -> (
    None
):
    binding_id = new_uuid7()
    first_order = _order(binding_id)
    second_order = _order(
        binding_id,
        organization_number="54321",
        order_number="0000000043",
        symbol="000660",
        cumulative_filled_quantity=Decimal("10"),
        average_fill_price=Decimal("120000"),
        total_filled_amount=Decimal("1200000"),
        remaining_quantity=Decimal("0"),
        fee_amount=Decimal("120"),
    )
    source = Source(
        {
            None: KisDailyOrderPage(
                binding_id=binding_id,
                provider_trade_date=date(2026, 8, 24),
                orders=(first_order,),
                next_cursor="page-2",
            ),
            "page-2": KisDailyOrderPage(
                binding_id=binding_id,
                provider_trade_date=date(2026, 8, 24),
                orders=(second_order,),
                next_cursor=None,
            ),
        },
        _account(
            binding_id,
            holdings=(
                KisCashHolding("000660", Decimal("10"), Decimal("10")),
                KisCashHolding("005930", Decimal("10"), Decimal("6")),
            ),
        ),
    )
    store = Store()

    result = await reconcile_kis_cash(binding_id, AS_OF, source=source, store=store)

    assert result.status is KisCashReconciliationStatus.COMPLETE
    assert result.blockers == ()
    assert result.page_count == 2
    assert result.orders == (first_order, second_order)
    assert result.cash_buying_power == Decimal("5000000")
    assert result.open_order_count == 1
    assert result.cumulative_fee_amount == Decimal("148")
    assert source.cursors == [None, "page-2", None, "page-2"]
    assert store.persisted == [result]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pages", "blocker"),
    (
        ({None: RuntimeError("missing page")}, "PROVIDER_PAGE_FAILURE"),
        (
            {
                None: KisDailyOrderPage(
                    binding_id=new_uuid7(),
                    provider_trade_date=date(2026, 8, 24),
                    orders=(),
                    next_cursor=None,
                )
            },
            "ACCOUNT_SCOPE_MISMATCH",
        ),
    ),
)
async def test_missing_page_or_account_scope_mismatch_is_non_passing(
    pages: dict[str | None, KisDailyOrderPage | Exception], blocker: str
) -> None:
    binding_id = new_uuid7()
    source = Source(pages, _account(binding_id))
    store = Store()

    result = await reconcile_kis_cash(binding_id, AS_OF, source=source, store=store)

    assert result.status is KisCashReconciliationStatus.NON_PASSING
    assert blocker in result.blockers
    assert store.persisted == []


@pytest.mark.asyncio
async def test_repeated_pagination_cursor_is_non_passing() -> None:
    binding_id = new_uuid7()
    page = KisDailyOrderPage(
        binding_id=binding_id,
        provider_trade_date=date(2026, 8, 24),
        orders=(),
        next_cursor="repeat",
    )
    source = Source({None: page, "repeat": page}, _account(binding_id))
    store = Store()

    result = await reconcile_kis_cash(binding_id, AS_OF, source=source, store=store)

    assert result.status is KisCashReconciliationStatus.NON_PASSING
    assert result.blockers == ("REPEATED_PROVIDER_CURSOR",)
    assert store.persisted == []


@pytest.mark.asyncio
async def test_provider_day_mismatch_is_non_passing_at_kst_boundary() -> None:
    binding_id = new_uuid7()
    source = Source(
        {
            None: KisDailyOrderPage(
                binding_id=binding_id,
                provider_trade_date=date(2026, 8, 23),
                orders=(),
                next_cursor=None,
            )
        },
        _account(binding_id),
    )

    result = await reconcile_kis_cash(binding_id, AS_OF, source=source, store=Store())

    assert result.status is KisCashReconciliationStatus.NON_PASSING
    assert result.provider_trade_date == date(2026, 8, 24)
    assert result.blockers == ("PROVIDER_DAY_MISMATCH",)


@pytest.mark.asyncio
async def test_missing_fill_fee_is_explicitly_non_passing() -> None:
    binding_id = new_uuid7()
    order = _order(binding_id, fee_amount=None)
    source = Source(
        {
            None: KisDailyOrderPage(
                binding_id=binding_id,
                provider_trade_date=date(2026, 8, 24),
                orders=(order,),
                next_cursor=None,
            )
        },
        _account(binding_id),
    )

    result = await reconcile_kis_cash(binding_id, AS_OF, source=source, store=Store())

    assert result.status is KisCashReconciliationStatus.NON_PASSING
    assert result.blockers == ("FILL_FEE_EVIDENCE_MISSING",)


@pytest.mark.asyncio
async def test_stale_or_wrong_account_snapshot_is_non_passing() -> None:
    binding_id = new_uuid7()
    page = KisDailyOrderPage(
        binding_id=binding_id,
        provider_trade_date=date(2026, 8, 24),
        orders=(),
        next_cursor=None,
    )
    source = Source(
        {None: page},
        _account(new_uuid7(), observed_at=AS_OF - timedelta(seconds=31), holdings=()),
    )

    result = await reconcile_kis_cash(binding_id, AS_OF, source=source, store=Store())

    assert result.status is KisCashReconciliationStatus.NON_PASSING
    assert result.blockers == (
        "ACCOUNT_FACT_SCOPE_MISMATCH",
        "ACCOUNT_FACT_STALE",
    )


@pytest.mark.asyncio
async def test_changed_second_pass_is_non_passing_and_not_persisted() -> None:
    binding_id = new_uuid7()
    first = _order(binding_id)
    changed = _order(
        binding_id,
        cumulative_filled_quantity=Decimal("5"),
        average_fill_price=Decimal("69900"),
        total_filled_amount=Decimal("349500"),
        remaining_quantity=Decimal("5"),
        fee_amount=Decimal("35"),
    )

    class ChangingSource(Source):
        async def read_daily_order_page(
            self, binding_id: UUID, trade_date: date, cursor: str | None
        ) -> KisDailyOrderPage:
            del trade_date
            self.cursors.append(cursor)
            order = first if len(self.cursors) == 1 else changed
            return KisDailyOrderPage(
                binding_id=binding_id,
                provider_trade_date=date(2026, 8, 24),
                orders=(order,),
                next_cursor=None,
            )

    source = ChangingSource({}, _account(binding_id))
    store = Store()

    result = await reconcile_kis_cash(binding_id, AS_OF, source=source, store=store)

    assert result.status is KisCashReconciliationStatus.NON_PASSING
    assert result.blockers == ("UNSTABLE_ORDER_SNAPSHOT",)
    assert store.persisted == []


@pytest.mark.asyncio
async def test_exact_restart_has_stable_content_digest_not_random_run_identity() -> (
    None
):
    binding_id = new_uuid7()
    page = KisDailyOrderPage(
        binding_id=binding_id,
        provider_trade_date=date(2026, 8, 24),
        orders=(),
        next_cursor=None,
    )

    first = await reconcile_kis_cash(
        binding_id,
        AS_OF,
        source=Source({None: page}, _account(binding_id, holdings=())),
        store=Store(),
    )
    restarted = await reconcile_kis_cash(
        binding_id,
        AS_OF,
        source=Source({None: page}, _account(binding_id, holdings=())),
        store=Store(),
    )

    assert first.reconciliation_id != restarted.reconciliation_id
    assert first.result_digest == restarted.result_digest
