from __future__ import annotations

from uuid import UUID

from autotrader.domain.enums import IntentType, OrderStyle, Side
from autotrader.execution.intents.models import (
    AccountCandidate,
    IntentOrigin,
    MarketQuote,
    OperatorRequest,
    OrderIntent,
    OrderTerms,
    ProtectionRequest,
    ReconciliationRequest,
    SizingApproved,
    StrategyIntentRequest,
)
from autotrader.shared.decimal import require_decimal
from autotrader.strategies.common.decisions import StrategyDecision


class AccountRouter:
    def route(
        self,
        candidates: tuple[AccountCandidate, ...],
        *,
        broker_code: str,
        market_code: str,
        environment: str,
        policy_key: str,
    ) -> AccountCandidate:
        matches = tuple(
            account
            for account in candidates
            if account.enabled
            and account.policy_active
            and account.broker_code == broker_code
            and account.market_code == market_code
            and account.environment == environment
            and account.policy_key == policy_key
        )
        if len(matches) != 1:
            raise ValueError("exactly one enabled account is required")
        return matches[0]


class OrderIntentFactory:
    def from_strategy_request(
        self, *, request: StrategyIntentRequest, account: AccountCandidate
    ) -> OrderIntent:
        return self._from_non_strategy(
            origin=IntentOrigin.STRATEGY,
            source_id=request.source_id,
            account=account,
            instrument_id=request.instrument_id,
            intent_type=request.intent_type,
            side=request.side,
            order_style=request.order_style,
            terms=request.terms,
            quote=None,
        )

    def from_strategy_decision(
        self,
        *,
        decision: StrategyDecision,
        account: AccountCandidate,
        sizing: SizingApproved,
        quote: MarketQuote | None = None,
    ) -> OrderIntent:
        self._require_enabled_account(account)
        if (
            decision.side is Side.BUY
            and decision.invalidation_price >= decision.planned_entry
        ) or (
            decision.side is Side.SELL
            and decision.invalidation_price <= decision.planned_entry
        ):
            raise ValueError("strategy invalidation must be protective")
        quantity = require_decimal(sizing.quantity)
        if quantity <= 0:
            raise ValueError("approved quantity must be positive")
        if decision.order_style is OrderStyle.LIMIT:
            if require_decimal(decision.planned_entry) <= 0:
                raise ValueError("positive limit price is required")
        else:
            if quote is None or not quote.fresh:
                raise ValueError("fresh side-specific quote is required")
            side_quote = quote.ask if decision.side is Side.BUY else quote.bid
            if require_decimal(side_quote) <= 0:
                raise ValueError("positive side-specific quote is required")
        return OrderIntent(
            origin=IntentOrigin.STRATEGY,
            source_id=decision.id,
            account_id=account.id,
            instrument_id=decision.instrument_id,
            intent_type=decision.intent_type,
            side=decision.side,
            order_style=decision.order_style,
            quantity=quantity,
            limit_price=decision.planned_entry
            if decision.order_style is OrderStyle.LIMIT
            else None,
            idempotency_key=self.identity(
                IntentOrigin.STRATEGY, decision.id, account.id
            ),
        )

    def from_operator(
        self, *, request: OperatorRequest, account: AccountCandidate
    ) -> OrderIntent:
        return self._from_non_strategy(
            origin=IntentOrigin.OPERATOR,
            source_id=request.audit_id,
            account=account,
            instrument_id=request.instrument_id,
            intent_type=request.intent_type,
            side=request.side,
            order_style=request.order_style,
            terms=request.terms,
            quote=request.quote,
        )

    def from_protection(
        self, *, request: ProtectionRequest, account: AccountCandidate
    ) -> OrderIntent:
        return self._from_non_strategy(
            # The reason is part of the identity here: one position carries a
            # structural stop, its exits and an emergency close, and they are
            # not the same request.
            discriminator=request.reason_code,
            origin=IntentOrigin.PROTECTION,
            source_id=request.locked_position_id,
            account=account,
            instrument_id=request.instrument_id,
            intent_type=request.intent_type,
            side=request.side,
            order_style=request.order_style,
            terms=request.terms,
            quote=request.quote,
        )

    def from_reconciliation(
        self, *, request: ReconciliationRequest, account: AccountCandidate
    ) -> OrderIntent:
        return self._from_non_strategy(
            origin=IntentOrigin.RECONCILIATION,
            source_id=request.blocking_diff_id,
            account=account,
            instrument_id=request.instrument_id,
            intent_type=request.intent_type,
            side=request.side,
            order_style=request.order_style,
            terms=request.terms,
            quote=request.quote,
        )

    @staticmethod
    def _from_non_strategy(
        *,
        origin: IntentOrigin,
        source_id: UUID,
        account: AccountCandidate,
        instrument_id: UUID,
        intent_type: IntentType,
        side: Side,
        order_style: OrderStyle,
        terms: OrderTerms,
        quote: MarketQuote | None,
        discriminator: str | None = None,
    ) -> OrderIntent:
        OrderIntentFactory._require_enabled_account(account)
        approved = require_decimal(terms.requested_quantity)
        if approved <= 0:
            raise ValueError("requested quantity must be positive")
        if order_style is OrderStyle.LIMIT and (
            terms.limit_price is None or require_decimal(terms.limit_price) <= 0
        ):
            raise ValueError("positive limit price is required")
        if order_style is OrderStyle.MARKET and terms.trigger_price is None:
            # A market order goes to the market now, so the price it will get
            # has to be known now. A stop does not: it waits, and its trigger
            # is the price that decides when it stops waiting.
            if quote is None or not quote.fresh:
                raise ValueError("fresh side-specific quote is required")
            side_quote = quote.ask if side is Side.BUY else quote.bid
            if require_decimal(side_quote) <= 0:
                raise ValueError("positive side-specific quote is required")
        return OrderIntent(
            origin=origin,
            source_id=source_id,
            account_id=account.id,
            instrument_id=instrument_id,
            intent_type=intent_type,
            side=side,
            order_style=order_style,
            quantity=approved,
            limit_price=terms.limit_price,
            idempotency_key=OrderIntentFactory.identity(
                origin, source_id, account.id, discriminator
            ),
            trigger_price=terms.trigger_price,
        )

    @staticmethod
    def identity(
        origin: IntentOrigin,
        source_id: UUID,
        account_id: UUID,
        discriminator: str | None = None,
    ) -> str:
        """What makes two requests the same request.

        A protection origin needs the discriminator. Its source is the
        position, and one position is protected by more than one kind of
        thing: a structural stop, a full exit, an emergency close. Keyed on
        the position alone they collide, and `create_or_get` hands the second
        one the first one's intent - so an emergency close would go out as
        whatever the stop was.
        """
        key = f"{origin.lower()}:{source_id.hex}:{account_id.hex}"
        return key if discriminator is None else f"{key}:{discriminator}"

    @staticmethod
    def _require_enabled_account(account: AccountCandidate) -> None:
        if not account.enabled or not account.policy_active:
            raise ValueError("enabled account with active policy is required")
