from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid7

from autotrader.domain.enums import IntentType, OrderStyle, Side
from autotrader.execution.intents.models import IntentOrigin, OrderIntent
from autotrader.risk.engine import RiskEngine
from autotrader.risk.models import (
    LockedAccountSnapshot,
    LockedPosition,
    RiskBudgetAnchorView,
    RiskContext,
    RiskOutcome,
    RiskPolicySnapshot,
    RiskQuote,
    RiskSnapshotView,
    TradingControlSnapshot,
)

NOW = datetime(2026, 8, 9, 9, tzinfo=UTC)


def intent(
    *,
    side: Side = Side.BUY,
    quantity: Decimal = Decimal("2"),
    order_style: OrderStyle = OrderStyle.LIMIT,
) -> OrderIntent:
    return OrderIntent(
        origin=IntentOrigin.STRATEGY,
        source_id=uuid7(),
        account_id=ACCOUNT_ID,
        instrument_id=INSTRUMENT_ID,
        intent_type=IntentType.ENTRY,
        side=side,
        order_style=order_style,
        quantity=quantity,
        limit_price=Decimal("10"),
        idempotency_key=f"strategy:{uuid7().hex}:{ACCOUNT_ID.hex}",
    )


ACCOUNT_ID = uuid7()
INSTRUMENT_ID = uuid7()


def context(**overrides: object) -> RiskContext:
    account = LockedAccountSnapshot(
        account_id=ACCOUNT_ID,
        environment="PAPER",
        equity=Decimal("1000"),
        cash=Decimal("1000"),
        buying_power=Decimal("1000"),
        currency="USD",
        captured_at=NOW,
        row_version=1,
    )
    risk_snapshot = RiskSnapshotView(
        id=uuid7(),
        account_id=ACCOUNT_ID,
        as_of=NOW,
        currency="USD",
        equity=Decimal("1000"),
        cash=Decimal("1000"),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        open_risk=Decimal("0"),
        daily_realized_pnl=Decimal("0"),
        daily_unrealized_pnl=Decimal("0"),
        drawdown=Decimal("0"),
        position_hash=b"p" * 32,
        open_order_hash=b"o" * 32,
    )
    values: dict[str, object] = {
        "decision_at": NOW,
        "account_snapshot": account,
        "risk_snapshot": risk_snapshot,
        "positions": (),
        "open_orders": (),
        "active_reservations": (),
        "budget_anchors": (
            RiskBudgetAnchorView(
                scope_type="GLOBAL",
                scope_key="GLOBAL",
                currency="USD",
                position_risk_amount=Decimal("0"),
                remaining_reservation_amount=Decimal("0"),
                hard_limit_amount=Decimal("500"),
                row_version=1,
            ),
            RiskBudgetAnchorView(
                scope_type="ACCOUNT",
                scope_key=str(ACCOUNT_ID),
                currency="USD",
                position_risk_amount=Decimal("0"),
                remaining_reservation_amount=Decimal("0"),
                hard_limit_amount=Decimal("500"),
                row_version=1,
            ),
        ),
        "quote": RiskQuote(
            instrument_id=INSTRUMENT_ID,
            bid=Decimal("9"),
            ask=Decimal("10"),
            currency="USD",
            as_of=NOW,
        ),
        "active_policy": RiskPolicySnapshot(
            policy_version_id=uuid7(),
            active=True,
            max_total_risk=Decimal("500"),
            max_position_value=Decimal("500"),
            max_daily_loss=Decimal("100"),
            max_drawdown=Decimal("100"),
            max_slippage_bps=Decimal("10"),
            max_account_snapshot_age=timedelta(minutes=1),
            max_risk_snapshot_age=timedelta(minutes=1),
            max_market_data_age=timedelta(seconds=30),
        ),
        "trading_control": TradingControlSnapshot(trading_enabled=True),
        "blocking_incident_count": 0,
        "unresolved_unknown_count": 0,
        "blocking_reconciliation_count": 0,
        "position_hash": b"p" * 32,
        "open_order_hash": b"o" * 32,
    }
    values.update(overrides)
    return RiskContext(**values)  # type: ignore[arg-type]


def test_approves_fresh_buy_with_matching_locked_context() -> None:
    decision = RiskEngine().evaluate(intent=intent(), context=context())

    assert decision.outcome is RiskOutcome.APPROVE
    assert decision.approved_quantity == Decimal("2")
    assert decision.reserved_risk_amount == Decimal("20")
    assert len(decision.decision_hash) == 32


def test_rejects_stale_quote_and_blocking_control() -> None:
    stale_quote = RiskQuote(
        instrument_id=INSTRUMENT_ID,
        bid=Decimal("9"),
        ask=Decimal("10"),
        currency="USD",
        as_of=NOW - timedelta(minutes=1),
    )
    stale = RiskEngine().evaluate(intent=intent(), context=context(quote=stale_quote))
    blocked = RiskEngine().evaluate(
        intent=intent(),
        context=context(trading_control=TradingControlSnapshot(trading_enabled=False)),
    )

    assert stale.outcome is RiskOutcome.REJECT
    assert stale.reason_codes == ("STALE_QUOTE",)
    assert blocked.reason_codes == ("TRADING_CONTROL_BLOCKED",)


def test_rejects_inactive_policy_and_negative_blocker_count() -> None:
    inactive = RiskEngine().evaluate(
        intent=intent(),
        context=context(
            active_policy=RiskPolicySnapshot(
                policy_version_id=uuid7(),
                active=False,
                max_total_risk=Decimal("500"),
                max_position_value=Decimal("500"),
                max_daily_loss=Decimal("100"),
                max_drawdown=Decimal("100"),
                max_slippage_bps=Decimal("10"),
                max_account_snapshot_age=timedelta(minutes=1),
                max_risk_snapshot_age=timedelta(minutes=1),
                max_market_data_age=timedelta(seconds=30),
            )
        ),
    )
    invalid_count = RiskEngine().evaluate(
        intent=intent(), context=context(blocking_incident_count=-1)
    )

    assert inactive.reason_codes == ("INACTIVE_POLICY",)
    assert invalid_count.reason_codes == ("INVALID_BLOCKER_COUNT",)


def test_market_reservation_uses_policy_slippage_worst_case_price() -> None:
    decision = RiskEngine().evaluate(
        intent=intent(order_style=OrderStyle.MARKET), context=context()
    )

    assert decision.reserved_risk_amount == Decimal("20.020")


def test_rejects_averaging_down_but_allows_reduce_only_sell() -> None:
    position = LockedPosition(
        instrument_id=INSTRUMENT_ID,
        quantity=Decimal("5"),
        available_quantity=Decimal("5"),
        average_cost=Decimal("11"),
        position_risk_amount=Decimal("50"),
        currency="USD",
        row_version=1,
    )
    average_down = RiskEngine().evaluate(
        intent=intent(side=Side.BUY), context=context(positions=(position,))
    )
    reduction = RiskEngine().evaluate(
        intent=intent(side=Side.SELL), context=context(positions=(position,))
    )

    assert average_down.reason_codes == ("AVERAGING_DOWN_BLOCKED",)
    assert reduction.outcome is RiskOutcome.REDUCE
    assert reduction.reserved_risk_amount == Decimal("0")
