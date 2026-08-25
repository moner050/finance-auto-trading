from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from autotrader.domain.enums import OrderStyle, Side
from autotrader.integrations.brokers.kis.cash_order_recovery import (
    KisAmbiguousDispatch,
    KisDailyOrder,
    KisRecoveryStatus,
    recover_ambiguous_cash_order,
)
from autotrader.shared.ids import new_uuid7

NOW = datetime(2026, 8, 24, 0, 15, 1, tzinfo=UTC)


def _dispatch(**changes: object) -> KisAmbiguousDispatch:
    values: dict[str, object] = {
        "dispatch_id": new_uuid7(),
        "binding_id": new_uuid7(),
        "side": Side.BUY,
        "symbol": "005930",
        "order_style": OrderStyle.LIMIT,
        "quantity": Decimal("10"),
        "limit_price": Decimal("70000"),
        "provider_window_start": NOW,
        "provider_window_end": NOW + timedelta(seconds=10),
        "request_digest": b"r" * 32,
    }
    values.update(changes)
    return KisAmbiguousDispatch(**values)  # type: ignore[arg-type]


def _order(dispatch: KisAmbiguousDispatch, **changes: object) -> KisDailyOrder:
    values: dict[str, object] = {
        "binding_id": dispatch.binding_id,
        "order_date": "20260824",
        "organization_number": "12345",
        "order_number": "0000000042",
        "original_order_number": "0000000000",
        "provider_timestamp": NOW + timedelta(seconds=5),
        "side": dispatch.side,
        "symbol": dispatch.symbol,
        "order_style": dispatch.order_style,
        "order_quantity": dispatch.quantity,
        "limit_price": dispatch.limit_price,
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


@pytest.mark.asyncio
async def test_zero_exact_candidates_remains_unknown_with_comparison_evidence() -> None:
    dispatch = _dispatch()
    wrong_symbol = _order(dispatch, symbol="000660")

    decision = await recover_ambiguous_cash_order(dispatch, (wrong_symbol,))

    assert decision.status is KisRecoveryStatus.UNKNOWN
    assert decision.reason == "ZERO_EXACT_CANDIDATES"
    assert decision.adopted_order is None
    assert decision.compared_candidate_digests == (wrong_symbol.record_digest,)


@pytest.mark.asyncio
async def test_one_exact_partial_fill_candidate_is_adopted() -> None:
    dispatch = _dispatch()
    exact = _order(dispatch)

    decision = await recover_ambiguous_cash_order(dispatch, (exact,))

    assert decision.status is KisRecoveryStatus.ADOPTED
    assert decision.reason == "UNIQUE_EXACT_CANDIDATE"
    assert decision.adopted_order == exact
    assert decision.adopted_order.cumulative_filled_quantity == Decimal("4")
    assert decision.adopted_order.remaining_quantity == Decimal("6")


@pytest.mark.asyncio
async def test_multiple_exact_candidates_remain_unknown() -> None:
    dispatch = _dispatch()
    first = _order(dispatch)
    second = _order(
        dispatch,
        organization_number="54321",
        order_number="0000000043",
        provider_timestamp=NOW + timedelta(seconds=6),
    )

    decision = await recover_ambiguous_cash_order(dispatch, (first, second))

    assert decision.status is KisRecoveryStatus.UNKNOWN
    assert decision.reason == "NON_UNIQUE_EXACT_CANDIDATES"
    assert decision.adopted_order is None
    assert decision.compared_candidate_digests == (
        first.record_digest,
        second.record_digest,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    (
        {"binding_id": new_uuid7()},
        {"side": Side.SELL},
        {"order_quantity": Decimal("11"), "remaining_quantity": Decimal("7")},
        {"limit_price": Decimal("70001")},
        {"provider_timestamp": NOW - timedelta(seconds=1)},
        {"provider_timestamp": NOW + timedelta(seconds=11)},
    ),
)
async def test_every_recovery_dimension_must_match(changes: dict[str, object]) -> None:
    dispatch = _dispatch()
    candidate = _order(dispatch, **changes)

    decision = await recover_ambiguous_cash_order(dispatch, (candidate,))

    assert decision.status is KisRecoveryStatus.UNKNOWN
    assert decision.reason == "ZERO_EXACT_CANDIDATES"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_changes",
    (
        {
            "cumulative_filled_quantity": Decimal("0"),
            "average_fill_price": Decimal("0"),
            "total_filled_amount": Decimal("0"),
            "confirmed_cancelled_quantity": Decimal("10"),
            "remaining_quantity": Decimal("0"),
            "fee_amount": Decimal("0"),
        },
        {
            "cumulative_filled_quantity": Decimal("0"),
            "average_fill_price": Decimal("0"),
            "total_filled_amount": Decimal("0"),
            "rejected_quantity": Decimal("10"),
            "remaining_quantity": Decimal("0"),
            "fee_amount": Decimal("0"),
        },
    ),
)
async def test_unique_cancelled_or_rejected_order_identity_is_still_adoptable(
    terminal_changes: dict[str, object],
) -> None:
    dispatch = _dispatch()
    order = _order(dispatch, **terminal_changes)

    decision = await recover_ambiguous_cash_order(dispatch, (order,))

    assert decision.status is KisRecoveryStatus.ADOPTED
    assert decision.adopted_order == order


def test_daily_order_rejects_inconsistent_cumulative_accounting() -> None:
    dispatch = _dispatch()

    with pytest.raises(ValueError, match="quantity accounting"):
        _order(dispatch, remaining_quantity=Decimal("5"))
