from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from autotrader.domain.enums import OrderStyle, Side
from autotrader.execution.intents.models import OrderIntent
from autotrader.risk.models import (
    RiskBudgetAnchorView,
    RiskContext,
    RiskDecision,
    RiskOutcome,
)
from autotrader.shared.ids import new_uuid7
from autotrader.shared.time import require_utc


class RiskEngine:
    def evaluate(self, *, intent: OrderIntent, context: RiskContext) -> RiskDecision:
        reason = self._context_rejection(intent, context)
        if reason is not None:
            return self._decision(
                intent, context, RiskOutcome.REJECT, reason, Decimal(0)
            )

        price = self._price(intent, context)
        position = next(
            (
                item
                for item in context.positions
                if item.instrument_id == intent.instrument_id
            ),
            None,
        )
        if intent.side is Side.SELL:
            if position is None or position.available_quantity < intent.quantity:
                return self._decision(
                    intent,
                    context,
                    RiskOutcome.REJECT,
                    "INSUFFICIENT_SELLABLE_QUANTITY",
                    Decimal(0),
                )
            return self._decision(intent, context, RiskOutcome.REDUCE, None, Decimal(0))

        if (
            position is not None
            and position.quantity > 0
            and price < position.average_cost
        ):
            return self._decision(
                intent,
                context,
                RiskOutcome.REJECT,
                "AVERAGING_DOWN_BLOCKED",
                Decimal(0),
            )

        reserved = intent.quantity * price
        if (
            reserved > context.account_snapshot.cash
            or reserved > context.account_snapshot.buying_power
        ):
            return self._decision(
                intent, context, RiskOutcome.REJECT, "INSUFFICIENT_CASH", Decimal(0)
            )
        current_value = Decimal(0) if position is None else position.quantity * price
        if current_value + reserved > context.active_policy.max_position_value:
            return self._decision(
                intent, context, RiskOutcome.REJECT, "CONCENTRATION_LIMIT", Decimal(0)
            )
        if self._exceeds_anchor_limit(
            context.budget_anchors, reserved, context.active_policy.max_total_risk
        ):
            return self._decision(
                intent, context, RiskOutcome.REJECT, "TOTAL_OPEN_RISK_LIMIT", Decimal(0)
            )
        return self._decision(intent, context, RiskOutcome.APPROVE, None, reserved)

    @staticmethod
    def _price(intent: OrderIntent, context: RiskContext) -> Decimal:
        if intent.order_style is OrderStyle.LIMIT:
            assert intent.limit_price is not None
            return intent.limit_price
        slippage = context.active_policy.max_slippage_bps / Decimal("10000")
        return (
            context.quote.ask * (Decimal(1) + slippage)
            if intent.side is Side.BUY
            else context.quote.bid * (Decimal(1) - slippage)
        )

    def _context_rejection(
        self, intent: OrderIntent, context: RiskContext
    ) -> str | None:
        decision_at = require_utc(context.decision_at)
        policy = context.active_policy
        if (
            intent.account_id != context.account_snapshot.account_id
            or context.risk_snapshot.account_id != intent.account_id
        ):
            return "ACCOUNT_SNAPSHOT_MISMATCH"
        if not policy.active:
            return "INACTIVE_POLICY"
        if (
            min(
                context.blocking_incident_count,
                context.blocking_reconciliation_count,
                context.unresolved_unknown_count,
            )
            < 0
        ):
            return "INVALID_BLOCKER_COUNT"
        if not context.trading_control.trading_enabled:
            return "TRADING_CONTROL_BLOCKED"
        if context.blocking_incident_count > 0:
            return "BLOCKING_INCIDENT"
        if context.blocking_reconciliation_count > 0:
            return "BLOCKING_RECONCILIATION"
        if context.unresolved_unknown_count > 0:
            return "UNRESOLVED_UNKNOWN"
        if (
            decision_at - require_utc(context.account_snapshot.captured_at)
            > policy.max_account_snapshot_age
        ):
            return "STALE_ACCOUNT_SNAPSHOT"
        if (
            decision_at - require_utc(context.risk_snapshot.as_of)
            > policy.max_risk_snapshot_age
        ):
            return "STALE_RISK_SNAPSHOT"
        if decision_at - require_utc(context.quote.as_of) > policy.max_market_data_age:
            return "STALE_QUOTE"
        if context.quote.instrument_id != intent.instrument_id:
            return "QUOTE_INSTRUMENT_MISMATCH"
        if context.quote.bid <= 0 or context.quote.ask <= 0:
            return "INVALID_QUOTE"
        if policy.max_slippage_bps < 0 or policy.max_slippage_bps >= Decimal("10000"):
            return "INVALID_SLIPPAGE_POLICY"
        if (
            context.risk_snapshot.position_hash != context.position_hash
            or context.risk_snapshot.open_order_hash != context.open_order_hash
        ):
            return "RISK_SNAPSHOT_HASH_MISMATCH"
        if (
            context.account_snapshot.currency != context.risk_snapshot.currency
            or context.quote.currency != context.risk_snapshot.currency
        ):
            return "CURRENCY_MISMATCH"
        if self._matching_anchors(context) is None:
            return "BUDGET_ANCHOR_MISMATCH"
        if (
            -(
                context.risk_snapshot.daily_realized_pnl
                + context.risk_snapshot.daily_unrealized_pnl
            )
            > policy.max_daily_loss
        ):
            return "DAILY_LOSS_LIMIT"
        if context.risk_snapshot.drawdown > policy.max_drawdown:
            return "DRAWDOWN_LIMIT"
        return None

    @staticmethod
    def _matching_anchors(
        context: RiskContext,
    ) -> tuple[RiskBudgetAnchorView, RiskBudgetAnchorView] | None:
        global_anchors = [
            anchor
            for anchor in context.budget_anchors
            if anchor.scope_type == "GLOBAL" and anchor.scope_key == "GLOBAL"
        ]
        account_anchors = [
            anchor
            for anchor in context.budget_anchors
            if anchor.scope_type == "ACCOUNT"
            and anchor.scope_key == str(context.account_snapshot.account_id)
        ]
        if len(global_anchors) != 1 or len(account_anchors) != 1:
            return None
        expected_currency = context.risk_snapshot.currency
        if any(
            anchor.currency != expected_currency
            for anchor in (*global_anchors, *account_anchors)
        ):
            return None
        return global_anchors[0], account_anchors[0]

    def _exceeds_anchor_limit(
        self,
        anchors: tuple[RiskBudgetAnchorView, ...],
        proposed: Decimal,
        policy_limit: Decimal,
    ) -> bool:
        matching = self._matching_anchors_from(anchors)
        if matching is None:
            return True
        return any(
            anchor.position_risk_amount + anchor.remaining_reservation_amount + proposed
            > min(anchor.hard_limit_amount, policy_limit)
            for anchor in matching
        )

    @staticmethod
    def _matching_anchors_from(
        anchors: tuple[RiskBudgetAnchorView, ...],
    ) -> tuple[RiskBudgetAnchorView, ...] | None:
        if len(anchors) != 2:
            return None
        return anchors

    def _decision(
        self,
        intent: OrderIntent,
        context: RiskContext,
        outcome: RiskOutcome,
        reason: str | None,
        reserved: Decimal,
    ) -> RiskDecision:
        decision_currency = (
            context.risk_snapshot.currency
            if context.risk_snapshot.currency is not None
            else context.account_snapshot.currency
        )
        approved_quantity = (
            intent.quantity
            if outcome in {RiskOutcome.APPROVE, RiskOutcome.REDUCE}
            else Decimal(0)
        )
        approved_price = self._price(intent, context) if approved_quantity > 0 else None
        reasons = () if reason is None else (reason,)
        payload = {
            "intent_id": intent.id.hex,
            "canonical_intent_hash": self._intent_hash(intent).hex(),
            "policy_version_id": context.active_policy.policy_version_id.hex,
            "risk_snapshot_id": context.risk_snapshot.id.hex,
            "position_hash": context.position_hash.hex(),
            "open_order_hash": context.open_order_hash.hex(),
            "anchor_versions": tuple(
                anchor.row_version for anchor in self._matching_anchors(context) or ()
            ),
            "quote": {
                "instrument_id": context.quote.instrument_id.hex,
                "bid": str(context.quote.bid),
                "ask": str(context.quote.ask),
                "currency": context.quote.currency,
                "as_of": require_utc(context.quote.as_of).isoformat(),
            },
            "outcome": outcome.value,
            "requested_quantity": str(intent.quantity),
            "approved_quantity": str(approved_quantity),
            "approved_price": None if approved_price is None else str(approved_price),
            "reserved": str(reserved),
            "currency": decision_currency,
            "reasons": reasons,
            "decided_at": require_utc(context.decision_at).isoformat(),
        }
        return RiskDecision(
            id=new_uuid7(),
            order_intent_id=intent.id,
            risk_snapshot_id=context.risk_snapshot.id,
            outcome=outcome,
            requested_quantity=intent.quantity,
            reason_codes=reasons,
            approved_quantity=approved_quantity,
            approved_limit_price=approved_price,
            reserved_risk_amount=reserved,
            currency=decision_currency,
            policy_version_id=context.active_policy.policy_version_id,
            decided_at=context.decision_at,
            decision_hash=hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).digest(),
        )

    @staticmethod
    def _intent_hash(intent: OrderIntent) -> bytes:
        payload = {
            "id": intent.id.hex,
            "origin": intent.origin.value,
            "source_id": intent.source_id.hex,
            "account_id": intent.account_id.hex,
            "instrument_id": intent.instrument_id.hex,
            "intent_type": intent.intent_type.value,
            "side": intent.side.value,
            "order_style": intent.order_style.value,
            "quantity": str(intent.quantity),
            "limit_price": None
            if intent.limit_price is None
            else str(intent.limit_price),
            "idempotency_key": intent.idempotency_key,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).digest()
