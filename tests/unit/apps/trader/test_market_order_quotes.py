"""A market order has to know the price it will get.

`OrderIntentFactory` has always required it and the loop never supplied it, so
neither an entry nor an exit could build an intent at all. Nothing noticed:
the Shadow loop's execution port never reaches order creation, no entry was
ever decided, and the only test of a closing action used a zero quantity and
returned before the intent was built. §31.11.

These are the tests that would have caught it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid7

import pytest

from autotrader.apps.trader.quotes import BinanceBookQuotes
from autotrader.domain.enums import IntentType, OrderStyle, Side
from autotrader.execution.intents.models import (
    AccountCandidate,
    MarketQuote,
    OrderTerms,
    ProtectionRequest,
    SizingApproved,
)
from autotrader.execution.intents.service import OrderIntentFactory
from autotrader.strategies.common.decisions import StrategyDecision

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _account() -> AccountCandidate:
    return AccountCandidate(
        id=uuid7(),
        broker_code="BINANCE",
        market_code="CRYPTO",
        environment="LIVE",
        enabled=True,
        policy_key="risk",
        policy_active=True,
    )


def _decision() -> StrategyDecision:
    return StrategyDecision(
        id=uuid7(),
        strategy_version_id=uuid7(),
        setup_id=uuid7(),
        feature_snapshot_id=uuid7(),
        instrument_id=uuid7(),
        intent_type=IntentType.ENTRY,
        side=Side.BUY,
        order_style=OrderStyle.MARKET,
        planned_entry=Decimal("60000"),
        trigger_price=Decimal("60000"),
        invalidation_price=Decimal("59000"),
        generated_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
        session_type="EUROPE",
    )


def _exit(trigger: Decimal | None = None) -> ProtectionRequest:
    return ProtectionRequest(
        locked_position_id=uuid7(),
        reason_code="EXIT_FULL_FIB_66",
        instrument_id=uuid7(),
        intent_type=IntentType.EXIT,
        side=Side.SELL,
        order_style=OrderStyle.MARKET,
        terms=OrderTerms(
            requested_quantity=Decimal("0.002"),
            limit_price=None,
            trigger_price=trigger,
        ),
    )


def _quote(*, fresh: bool = True) -> MarketQuote:
    return MarketQuote(bid=Decimal("59999.9"), ask=Decimal("60000.1"), fresh=fresh)


def test_a_market_entry_needs_a_quote_and_builds_with_one() -> None:
    factory = OrderIntentFactory()
    account = _account()
    with pytest.raises(ValueError):
        factory.from_strategy_decision(
            decision=_decision(),
            account=account,
            sizing=SizingApproved(Decimal("0.002")),
        )
    intent = factory.from_strategy_decision(
        decision=_decision(),
        account=account,
        sizing=SizingApproved(Decimal("0.002")),
        quote=_quote(),
    )
    assert intent.order_style is OrderStyle.MARKET


def test_a_market_exit_needs_a_quote_and_builds_with_one() -> None:
    """The path that had no test at all: the only closing test used a zero
    quantity and returned before the intent was built."""
    factory = OrderIntentFactory()
    account = _account()
    with pytest.raises(ValueError):
        factory.from_protection(account=account, request=_exit())
    intent = factory.from_protection(
        account=account,
        request=ProtectionRequest(
            **{
                **{
                    field: getattr(_exit(), field)
                    for field in ProtectionRequest.__dataclass_fields__
                },
                "quote": _quote(),
            }
        ),
    )
    assert intent.intent_type is IntentType.EXIT


def test_a_stale_quote_is_no_quote() -> None:
    with pytest.raises(ValueError):
        OrderIntentFactory().from_strategy_decision(
            decision=_decision(),
            account=_account(),
            sizing=SizingApproved(Decimal("0.002")),
            quote=_quote(fresh=False),
        )


def test_a_protective_stop_needs_none_because_it_waits() -> None:
    """Which is why this defect never showed on the stop path."""
    intent = OrderIntentFactory().from_protection(
        account=_account(),
        request=ProtectionRequest(
            locked_position_id=uuid7(),
            reason_code="STRUCTURAL_STOP",
            instrument_id=uuid7(),
            intent_type=IntentType.PROTECTIVE,
            side=Side.SELL,
            order_style=OrderStyle.MARKET,
            terms=OrderTerms(
                requested_quantity=Decimal("0.002"),
                limit_price=None,
                trigger_price=Decimal("59000"),
            ),
        ),
    )
    assert intent.trigger_price == Decimal("59000")


class _Rest:
    def __init__(self, *, bid: str, ask: str) -> None:
        self._bid = bid
        self._ask = ask
        self.calls = 0

    async def book_ticker(self, *, symbol: str) -> dict[str, object]:
        self.calls += 1
        return {"symbol": symbol, "bidPrice": self._bid, "askPrice": self._ask}


@pytest.mark.asyncio
async def test_the_book_is_read_as_two_prices_not_one() -> None:
    """A bar's close in both bid and ask would say the spread is zero, which
    is the thing the rule exists to stop being assumed."""
    rest = _Rest(bid="59999.9", ask="60000.1")
    quotes = BinanceBookQuotes(rest=rest, symbol="BTCUSDT")  # pyright: ignore[reportArgumentType]
    quote = await quotes.quote()
    assert quote.bid == Decimal("59999.9")
    assert quote.ask == Decimal("60000.1")
    assert quote.bid < quote.ask
    assert quote.fresh is True


@pytest.mark.asyncio
async def test_a_read_that_took_too_long_is_not_fresh() -> None:
    """A price that arrived late is a price that may already be gone."""
    ticks = iter((100.0, 105.0))
    quotes = BinanceBookQuotes(
        rest=_Rest(bid="59999.9", ask="60000.1"),  # pyright: ignore[reportArgumentType]
        symbol="BTCUSDT",
        within=timedelta(seconds=2),
        monotonic=lambda: next(ticks),
    )
    assert (await quotes.quote()).fresh is False


def test_a_quote_source_refuses_a_window_that_is_not_one() -> None:
    for window in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError):
            BinanceBookQuotes(
                rest=_Rest(bid="1", ask="2"),  # pyright: ignore[reportArgumentType]
                symbol="BTCUSDT",
                within=window,
            )
