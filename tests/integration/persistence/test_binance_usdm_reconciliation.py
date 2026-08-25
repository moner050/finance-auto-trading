from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from autotrader.integrations.brokers.binance_usdm import (
    reconciliation as reconciliation_module,
)
from autotrader.integrations.brokers.binance_usdm.account import (
    BinanceUsdmAccountSnapshot,
    BinanceUsdmBalance,
    BinanceUsdmIncomeFact,
    BinanceUsdmOpenAlgoOrder,
    BinanceUsdmOpenOrder,
    BinanceUsdmPosition,
    BinanceUsdmTradeFact,
)
from autotrader.integrations.brokers.binance_usdm.reconciliation import (
    BinanceUsdmConfigurationFact,
    BinanceUsdmReconciliationCapture,
    BinanceUsdmReconciliationContext,
    BinanceUsdmReconciliationResult,
    reconcile_binance_usdm,
)
from autotrader.shared.ids import new_uuid7

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)


@dataclass
class Source:
    captures: list[BinanceUsdmReconciliationCapture]
    calls: list[datetime] = field(default_factory=list[datetime])

    async def capture(self, as_of: datetime) -> BinanceUsdmReconciliationCapture:
        self.calls.append(as_of)
        return self.captures.pop(0)


@dataclass
class Store:
    results: list[BinanceUsdmReconciliationResult] = field(
        default_factory=list[BinanceUsdmReconciliationResult]
    )

    async def persist(self, result: BinanceUsdmReconciliationResult) -> None:
        self.results.append(result)


def snapshot(
    *,
    income: Decimal = Decimal("-1.25"),
    client_order_id: str = "v6-019d0000000070008000000000000000",
    client_algo_id: str = "v6s-019d0000000070008000000000000000",
) -> BinanceUsdmAccountSnapshot:
    return BinanceUsdmAccountSnapshot(
        as_of=NOW,
        balances=(
            BinanceUsdmBalance(
                asset="USDT",
                balance=Decimal("10000"),
                available_balance=Decimal("9980"),
                maximum_withdraw_amount=Decimal("9980"),
                updated_at=NOW,
            ),
        ),
        positions=(
            BinanceUsdmPosition(
                symbol="BTCUSDT",
                position_side="BOTH",
                amount=Decimal("0"),
                entry_price=Decimal("0"),
                mark_price=Decimal("60000"),
                unrealized_pnl=Decimal("0"),
                isolated_margin=Decimal("0"),
                notional=Decimal("0"),
                margin_asset="USDT",
                initial_margin=Decimal("0"),
                maintenance_margin=Decimal("0"),
                position_initial_margin=Decimal("0"),
                open_order_initial_margin=Decimal("0"),
                updated_at=NOW,
            ),
        ),
        normal_orders=(
            BinanceUsdmOpenOrder(
                order_id=811,
                client_order_id=client_order_id,
                symbol="BTCUSDT",
                status="FILLED",
                side="BUY",
                order_type="MARKET",
                executed_quantity=Decimal("0.002"),
                original_quantity=Decimal("0.002"),
                reduce_only=False,
                close_position=False,
            ),
        ),
        algo_orders=(
            BinanceUsdmOpenAlgoOrder(
                algo_id=2146760,
                client_algo_id=client_algo_id,
                symbol="BTCUSDT",
                status="NEW",
                side="SELL",
                order_type="STOP_MARKET",
                quantity=Decimal("0"),
                trigger_price=Decimal("59000"),
                close_position=True,
            ),
        ),
        trades=(
            BinanceUsdmTradeFact(
                trade_id=91,
                order_id=811,
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("0.002"),
                price=Decimal("60000"),
                commission=Decimal("0.048"),
                commission_asset="USDT",
                realized_pnl=Decimal("0"),
                occurred_at=NOW - timedelta(seconds=2),
            ),
        ),
        income=(
            BinanceUsdmIncomeFact(
                transaction_id=501,
                trade_id="91",
                symbol="BTCUSDT",
                income_type="COMMISSION",
                income=Decimal("-0.048"),
                asset="USDT",
                occurred_at=NOW - timedelta(seconds=2),
            ),
            BinanceUsdmIncomeFact(
                transaction_id=502,
                trade_id="",
                symbol="BTCUSDT",
                income_type="FUNDING_FEE",
                income=income,
                asset="USDT",
                occurred_at=NOW - timedelta(seconds=1),
            ),
        ),
    )


def configuration(
    *,
    position_mode: str = "ONE_WAY",
    margin_type: str = "ISOLATED",
    auto_add_margin: bool = False,
    leverage: int = 3,
    can_trade: bool = True,
    multi_assets_margin: bool = False,
    transfer_out_enabled: bool = False,
) -> BinanceUsdmConfigurationFact:
    return BinanceUsdmConfigurationFact(
        position_mode=position_mode,
        margin_type=margin_type,
        auto_add_margin=auto_add_margin,
        leverage=leverage,
        can_trade=can_trade,
        multi_assets_margin=multi_assets_margin,
        transfer_out_enabled=transfer_out_enabled,
        maximum_notional=Decimal("5000000"),
        price_tick_size=Decimal("0.1"),
        minimum_quantity=Decimal("0.001"),
        quantity_step_size=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        captured_at=NOW,
    )


def capture(
    *,
    account: BinanceUsdmAccountSnapshot | None = None,
    config: BinanceUsdmConfigurationFact | None = None,
) -> BinanceUsdmReconciliationCapture:
    return BinanceUsdmReconciliationCapture(
        account=snapshot() if account is None else account,
        configuration=configuration() if config is None else config,
        captured_at=NOW,
    )


def context(
    source: Source,
    store: Store,
    *,
    account_id: UUID | None = None,
) -> BinanceUsdmReconciliationContext:
    return BinanceUsdmReconciliationContext(
        account_id=new_uuid7() if account_id is None else account_id,
        source=source,
        store=store,
        clock=lambda: NOW + timedelta(seconds=1),
        new_run_id=new_uuid7,
    )


@pytest.mark.asyncio
async def test_two_identical_complete_captures_persist_every_fact_category() -> None:
    first = capture()
    second = capture()
    source = Source([first, second])
    store = Store()

    result = await reconcile_binance_usdm(
        new_uuid7(),
        as_of=NOW,
        context=context(source, store),
    )

    assert source.calls == [NOW, NOW]
    assert result.state == "COMPLETE"
    assert result.stable is True
    assert result.blockers == ()
    assert len(result.fact_digest) == 32
    assert result.counts == {
        "balances": 1,
        "positions": 1,
        "normal_orders": 1,
        "algo_orders": 1,
        "trades": 1,
        "income": 2,
        "configuration": 1,
    }
    assert result.capture == second
    assert store.results == [result]


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["funding", "leverage"])
async def test_any_financial_or_configuration_drift_is_non_passing(
    drift: str,
) -> None:
    first = capture()
    if drift == "funding":
        second = capture(account=snapshot(income=Decimal("-1.26")))
    else:
        second = capture(config=configuration(leverage=4))
    store = Store()

    result = await reconcile_binance_usdm(
        new_uuid7(),
        as_of=NOW,
        context=context(Source([first, second]), store),
    )

    assert result.state == "PARTIAL"
    assert result.stable is False
    assert result.blockers == ("SNAPSHOT_DRIFT",)
    assert result.capture is None
    assert store.results == [result]


@pytest.mark.asyncio
async def test_normal_and_algo_client_id_namespaces_do_not_alias() -> None:
    shared = "same-provider-client-id"
    value = capture(account=snapshot(client_order_id=shared, client_algo_id=shared))

    result = await reconcile_binance_usdm(
        new_uuid7(),
        as_of=NOW,
        context=context(Source([value, value]), Store()),
    )

    assert result.state == "COMPLETE"
    assert result.capture is not None
    assert result.capture.account.normal_orders[0].client_order_id == shared
    assert result.capture.account.algo_orders[0].client_algo_id == shared


@pytest.mark.asyncio
async def test_unsafe_configuration_is_persisted_as_fact_for_readiness() -> None:
    unsafe = configuration(
        position_mode="HEDGE",
        margin_type="CROSSED",
        auto_add_margin=True,
        leverage=20,
        can_trade=False,
        multi_assets_margin=True,
        transfer_out_enabled=True,
    )
    value = capture(config=unsafe)

    result = await reconcile_binance_usdm(
        new_uuid7(),
        as_of=NOW,
        context=context(Source([value, value]), Store()),
    )

    assert result.state == "COMPLETE"
    assert result.capture is not None
    assert result.capture.configuration == unsafe


def test_capture_rejects_duplicate_position_identity() -> None:
    account = snapshot()

    with pytest.raises(ValueError, match="position identity"):
        capture(account=replace(account, positions=account.positions * 2))


def test_capture_rejects_trade_that_database_cannot_persist() -> None:
    account = snapshot()
    invalid_trade = replace(account.trades[0], commission=Decimal("-0.01"))

    with pytest.raises(ValueError, match="trade fact"):
        capture(account=replace(account, trades=(invalid_trade,)))


def test_capture_rejects_duplicate_income_identity() -> None:
    account = snapshot()
    duplicate = replace(
        account.income[1],
        transaction_id=account.income[0].transaction_id,
    )

    with pytest.raises(ValueError, match="income identity"):
        capture(account=replace(account, income=(account.income[0], duplicate)))


@pytest.mark.parametrize("kind", ("normal", "algo"))
def test_capture_rejects_unknown_provider_order_status(kind: str) -> None:
    account = snapshot()

    with pytest.raises(ValueError, match="order fact"):
        if kind == "normal":
            invalid = replace(account.normal_orders[0], status="NEW_PROVIDER_STATE")
            capture(account=replace(account, normal_orders=(invalid,)))
        else:
            invalid_algo = replace(
                account.algo_orders[0],
                status="NEW_PROVIDER_STATE",
            )
            capture(account=replace(account, algo_orders=(invalid_algo,)))


@pytest.mark.asyncio
async def test_second_capture_failure_never_persists_complete() -> None:
    class FailingSource(Source):
        async def capture(self, as_of: datetime) -> BinanceUsdmReconciliationCapture:
            if self.calls:
                raise RuntimeError("provider unavailable")
            return await super().capture(as_of)

    store = Store()
    result = await reconcile_binance_usdm(
        new_uuid7(),
        as_of=NOW,
        context=context(FailingSource([capture()]), store),
    )

    assert result.state == "PARTIAL"
    assert result.blockers == ("PROVIDER_UNAVAILABLE",)
    assert store.results == [result]


@pytest.mark.asyncio
async def test_capture_deadline_exceeded_persists_partial_run() -> None:
    value = capture()
    store = Store()
    slow_context = replace(
        context(Source([value, value]), store),
        clock=lambda: NOW + timedelta(seconds=31),
    )

    result = await reconcile_binance_usdm(
        new_uuid7(),
        as_of=NOW,
        context=slow_context,
    )

    assert result.state == "PARTIAL"
    assert result.completed_at == NOW + timedelta(seconds=31)
    assert result.blockers == ("CAPTURE_DEADLINE_EXCEEDED",)
    assert store.results == [result]


@pytest.mark.asyncio
async def test_hung_capture_times_out_and_persists_partial_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HangingSource(Source):
        async def capture(self, as_of: datetime) -> BinanceUsdmReconciliationCapture:
            self.calls.append(as_of)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    monkeypatch.setattr(
        reconciliation_module,
        "_MAX_CAPTURE_DURATION",
        timedelta(milliseconds=10),
    )
    store = Store()

    result = await asyncio.wait_for(
        reconcile_binance_usdm(
            new_uuid7(),
            as_of=NOW,
            context=context(HangingSource([]), store),
        ),
        timeout=0.1,
    )

    assert result.state == "PARTIAL"
    assert result.blockers == ("CAPTURE_DEADLINE_EXCEEDED",)
    assert store.results == [result]
