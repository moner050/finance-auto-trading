from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from autotrader.integrations.brokers.binance_usdm.account import (
    BinanceUsdmAccountSnapshot,
    BinanceUsdmIncomeFact,
    BinanceUsdmOpenAlgoOrder,
    BinanceUsdmOpenOrder,
    BinanceUsdmTradeFact,
)
from autotrader.persistence.mysql.models.binance_usdm import (
    BinanceUsdmAlgoOrderFactRow,
    BinanceUsdmBalanceFactRow,
    BinanceUsdmConfigurationFactRow,
    BinanceUsdmIncomeFactRow,
    BinanceUsdmOrderFactRow,
    BinanceUsdmPositionFactRow,
    BinanceUsdmReconciliationRunRow,
    BinanceUsdmTradeFactRow,
)
from autotrader.persistence.mysql.repositories.binance_usdm import (
    BinanceUsdmReconciliationRepository,
)
from autotrader.shared.ids import new_uuid7

_MAX_CAPTURE_DURATION = timedelta(seconds=30)
_SYMBOL = "BTCUSDT"
_NORMAL_ORDER_STATUSES = frozenset(
    {
        "NEW",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "REJECTED",
        "EXPIRED",
        "EXPIRED_IN_MATCH",
    }
)
_ALGO_ORDER_STATUSES = frozenset({"NEW", "CANCELED", "TRIGGERED", "FINISHED"})


class BinanceUsdmReconciliationUnavailable(RuntimeError):
    """An exact two-capture reconciliation could not be evaluated."""


@dataclass(frozen=True, slots=True)
class BinanceUsdmConfigurationFact:
    position_mode: str
    margin_type: str
    auto_add_margin: bool
    leverage: int
    can_trade: bool
    multi_assets_margin: bool
    transfer_out_enabled: bool
    maximum_notional: Decimal
    price_tick_size: Decimal
    minimum_quantity: Decimal
    quantity_step_size: Decimal
    minimum_notional: Decimal
    captured_at: datetime

    def __post_init__(self) -> None:
        if self.position_mode not in {"ONE_WAY", "HEDGE"}:
            raise ValueError("Binance USD-M position mode fact is invalid")
        if self.margin_type not in {"ISOLATED", "CROSSED"}:
            raise ValueError("Binance USD-M margin type fact is invalid")
        if type(self.auto_add_margin) is not bool:
            raise TypeError("Binance USD-M auto-add margin fact must be bool")
        if type(self.leverage) is not int or self.leverage <= 0:
            raise ValueError("Binance USD-M leverage fact must be positive")
        for name in ("can_trade", "multi_assets_margin", "transfer_out_enabled"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"Binance USD-M {name} fact must be bool")
        for name in (
            "maximum_notional",
            "price_tick_size",
            "minimum_quantity",
            "quantity_step_size",
            "minimum_notional",
        ):
            if _decimal(getattr(self, name), name) <= 0:
                raise ValueError(f"Binance USD-M {name} must be positive")
        _utc(self.captured_at, "configuration captured_at")


@dataclass(frozen=True, slots=True)
class BinanceUsdmReconciliationCapture:
    account: BinanceUsdmAccountSnapshot
    configuration: BinanceUsdmConfigurationFact
    captured_at: datetime

    def __post_init__(self) -> None:
        if type(self.account) is not BinanceUsdmAccountSnapshot:
            raise TypeError("Binance USD-M account snapshot must be exact")
        self.account.__post_init__()
        if type(self.configuration) is not BinanceUsdmConfigurationFact:
            raise TypeError("Binance USD-M configuration fact must be exact")
        self.configuration.__post_init__()
        captured_at = _utc(self.captured_at, "reconciliation captured_at")
        if self.account.as_of > captured_at:
            raise ValueError("Binance USD-M capture predates provider as-of")
        if self.configuration.captured_at > captured_at:
            raise ValueError("Binance USD-M capture predates configuration")
        _validate_account_scope(self.account, captured_at=captured_at)


class BinanceUsdmReconciliationSource(Protocol):
    async def capture(self, as_of: datetime) -> BinanceUsdmReconciliationCapture: ...


class BinanceUsdmReconciliationStore(Protocol):
    async def persist(self, result: BinanceUsdmReconciliationResult) -> None: ...


@dataclass(frozen=True, slots=True)
class BinanceUsdmReconciliationContext:
    account_id: UUID
    source: BinanceUsdmReconciliationSource = field(repr=False)
    store: BinanceUsdmReconciliationStore = field(repr=False)
    clock: Callable[[], datetime] = field(repr=False)
    new_run_id: Callable[[], UUID] = field(repr=False)

    def __post_init__(self) -> None:
        _uuid7(self.account_id, "account_id")
        if not callable(self.clock) or not callable(self.new_run_id):
            raise TypeError("Binance USD-M reconciliation callbacks are invalid")


@dataclass(frozen=True, slots=True)
class BinanceUsdmReconciliationResult:
    run_id: UUID
    binding_id: UUID
    account_id: UUID
    provider_as_of: datetime
    started_at: datetime
    completed_at: datetime
    state: str
    stable: bool
    fact_digest: bytes
    blockers: tuple[str, ...]
    capture: BinanceUsdmReconciliationCapture | None

    def __post_init__(self) -> None:
        for name in ("run_id", "binding_id", "account_id"):
            _uuid7(getattr(self, name), name)
        provider_as_of = _utc(self.provider_as_of, "provider_as_of")
        started_at = _utc(self.started_at, "started_at")
        completed_at = _utc(self.completed_at, "completed_at")
        if provider_as_of > started_at or started_at >= completed_at:
            raise ValueError("Binance USD-M reconciliation times are invalid")
        if type(self.fact_digest) is not bytes or len(self.fact_digest) != 32:
            raise ValueError("Binance USD-M fact digest must be SHA-256")
        if (
            type(self.blockers) is not tuple
            or any(type(code) is not str or not code for code in self.blockers)
            or tuple(sorted(set(self.blockers))) != self.blockers
        ):
            raise ValueError("Binance USD-M blockers must be canonical")
        if self.state == "COMPLETE":
            if (
                self.stable is not True
                or self.blockers
                or type(self.capture) is not BinanceUsdmReconciliationCapture
            ):
                raise ValueError("complete Binance USD-M reconciliation is invalid")
        elif self.state == "PARTIAL":
            if (
                self.stable is not False
                or not self.blockers
                or self.capture is not None
            ):
                raise ValueError("partial Binance USD-M reconciliation is invalid")
        else:
            raise ValueError("Binance USD-M reconciliation state is invalid")

    @property
    def counts(self) -> dict[str, int]:
        capture = self.capture
        if capture is None:
            return {
                "balances": 0,
                "positions": 0,
                "normal_orders": 0,
                "algo_orders": 0,
                "trades": 0,
                "income": 0,
                "configuration": 0,
            }
        account = capture.account
        return {
            "balances": len(account.balances),
            "positions": len(account.positions),
            "normal_orders": len(account.normal_orders),
            "algo_orders": len(account.algo_orders),
            "trades": len(account.trades),
            "income": len(account.income),
            "configuration": 1,
        }


@dataclass(frozen=True, slots=True)
class MySqlBinanceUsdmReconciliationStore:
    repository: BinanceUsdmReconciliationRepository = field(repr=False)

    async def persist(self, result: BinanceUsdmReconciliationResult) -> None:
        if type(result) is not BinanceUsdmReconciliationResult:
            raise TypeError("Binance USD-M reconciliation result must be exact")
        result.__post_init__()
        counts = result.counts
        run = BinanceUsdmReconciliationRunRow(
            id=result.run_id,
            binding_id=result.binding_id,
            account_id=result.account_id,
            provider_code="BINANCE",
            market_code="USD-M",
            symbol=_SYMBOL,
            settlement_asset="USDT",
            provider_as_of=result.provider_as_of,
            started_at=result.started_at,
            completed_at=result.completed_at,
            result=result.state,
            balance_fact_count=counts["balances"],
            position_fact_count=counts["positions"],
            order_fact_count=counts["normal_orders"],
            algo_order_fact_count=counts["algo_orders"],
            trade_fact_count=counts["trades"],
            income_fact_count=counts["income"],
            configuration_fact_count=counts["configuration"],
            fact_digest=result.fact_digest,
            blockers=list(result.blockers),
        )
        capture = result.capture
        if capture is None:
            await self.repository.persist_bundle(
                run=run,
                balances=(),
                positions=(),
                normal_orders=(),
                algo_orders=(),
                trades=(),
                income=(),
                configurations=(),
            )
            return
        account = capture.account
        await self.repository.persist_bundle(
            run=run,
            balances=tuple(
                BinanceUsdmBalanceFactRow(
                    id=new_uuid7(),
                    run_id=result.run_id,
                    asset=value.asset,
                    wallet_balance=value.balance,
                    available_balance=value.available_balance,
                    maximum_withdraw_amount=value.maximum_withdraw_amount,
                    updated_at=value.updated_at,
                    captured_at=capture.captured_at,
                )
                for value in account.balances
            ),
            positions=tuple(
                BinanceUsdmPositionFactRow(
                    id=new_uuid7(),
                    run_id=result.run_id,
                    symbol=value.symbol,
                    position_side=value.position_side,
                    amount=value.amount,
                    entry_price=value.entry_price,
                    mark_price=value.mark_price,
                    unrealized_pnl=value.unrealized_pnl,
                    isolated_margin=value.isolated_margin,
                    notional=value.notional,
                    margin_asset=value.margin_asset,
                    initial_margin=value.initial_margin,
                    maintenance_margin=value.maintenance_margin,
                    position_initial_margin=value.position_initial_margin,
                    open_order_initial_margin=value.open_order_initial_margin,
                    updated_at=value.updated_at,
                    captured_at=capture.captured_at,
                )
                for value in account.positions
            ),
            normal_orders=tuple(
                BinanceUsdmOrderFactRow(
                    id=new_uuid7(),
                    run_id=result.run_id,
                    provider_order_id=value.order_id,
                    client_order_id=value.client_order_id,
                    symbol=value.symbol,
                    status=value.status,
                    side=value.side,
                    order_type=value.order_type,
                    executed_quantity=value.executed_quantity,
                    original_quantity=value.original_quantity,
                    reduce_only=value.reduce_only,
                    close_position=value.close_position,
                    captured_at=capture.captured_at,
                )
                for value in account.normal_orders
            ),
            algo_orders=tuple(
                BinanceUsdmAlgoOrderFactRow(
                    id=new_uuid7(),
                    run_id=result.run_id,
                    provider_algo_id=value.algo_id,
                    client_algo_id=value.client_algo_id,
                    symbol=value.symbol,
                    status=value.status,
                    side=value.side,
                    order_type=value.order_type,
                    quantity=value.quantity,
                    trigger_price=value.trigger_price,
                    close_position=value.close_position,
                    captured_at=capture.captured_at,
                )
                for value in account.algo_orders
            ),
            trades=tuple(
                BinanceUsdmTradeFactRow(
                    id=new_uuid7(),
                    run_id=result.run_id,
                    provider_trade_id=value.trade_id,
                    provider_order_id=value.order_id,
                    symbol=value.symbol,
                    side=value.side,
                    quantity=value.quantity,
                    price=value.price,
                    commission=value.commission,
                    commission_asset=value.commission_asset,
                    realized_pnl=value.realized_pnl,
                    occurred_at=value.occurred_at,
                    captured_at=capture.captured_at,
                )
                for value in account.trades
            ),
            income=tuple(
                BinanceUsdmIncomeFactRow(
                    id=new_uuid7(),
                    run_id=result.run_id,
                    provider_transaction_id=value.transaction_id,
                    trade_id=value.trade_id,
                    symbol=value.symbol,
                    income_type=value.income_type,
                    income=value.income,
                    asset=value.asset,
                    occurred_at=value.occurred_at,
                    captured_at=capture.captured_at,
                )
                for value in account.income
            ),
            configurations=(
                BinanceUsdmConfigurationFactRow(
                    id=new_uuid7(),
                    run_id=result.run_id,
                    position_mode=capture.configuration.position_mode,
                    margin_type=capture.configuration.margin_type,
                    auto_add_margin=capture.configuration.auto_add_margin,
                    leverage=capture.configuration.leverage,
                    can_trade=capture.configuration.can_trade,
                    multi_assets_margin=capture.configuration.multi_assets_margin,
                    transfer_out_enabled=(capture.configuration.transfer_out_enabled),
                    maximum_notional=capture.configuration.maximum_notional,
                    price_tick_size=capture.configuration.price_tick_size,
                    minimum_quantity=capture.configuration.minimum_quantity,
                    quantity_step_size=capture.configuration.quantity_step_size,
                    minimum_notional=capture.configuration.minimum_notional,
                    captured_at=capture.configuration.captured_at,
                ),
            ),
        )


async def reconcile_binance_usdm(
    binding_id: UUID,
    *,
    as_of: datetime,
    context: BinanceUsdmReconciliationContext,
) -> BinanceUsdmReconciliationResult:
    try:
        _uuid7(binding_id, "binding_id")
        if type(context) is not BinanceUsdmReconciliationContext:
            raise TypeError
        context.__post_init__()
        provider_as_of = _utc(as_of, "provider as_of")
    except TypeError, ValueError:
        raise BinanceUsdmReconciliationUnavailable(
            "Binance USD-M reconciliation input is invalid"
        ) from None
    run_id = context.new_run_id()
    _uuid7(run_id, "run_id")
    first: BinanceUsdmReconciliationCapture | None = None
    try:
        async with asyncio.timeout(_MAX_CAPTURE_DURATION.total_seconds()):
            first = await _capture_once(context.source, provider_as_of)
            second = await _capture_once(context.source, provider_as_of)
    except asyncio.CancelledError, KeyboardInterrupt, SystemExit:
        raise
    except TimeoutError:
        completed_at = _capture_time(provider_as_of, context.clock)
        result = _partial_result(
            run_id=run_id,
            binding_id=binding_id,
            context=context,
            provider_as_of=provider_as_of,
            blocker="CAPTURE_DEADLINE_EXCEEDED",
            values=(() if first is None else (_projection_digest(first),)),
            completed_at=completed_at,
        )
        await context.store.persist(result)
        return result
    except Exception:
        completed_at = _capture_time(provider_as_of, context.clock)
        result = _partial_result(
            run_id=run_id,
            binding_id=binding_id,
            context=context,
            provider_as_of=provider_as_of,
            blocker="PROVIDER_UNAVAILABLE",
            values=(() if first is None else (_projection_digest(first),)),
            completed_at=completed_at,
        )
        await context.store.persist(result)
        return result
    first_digest = _projection_digest(first)
    second_digest = _projection_digest(second)
    completed_at = _capture_time(provider_as_of, context.clock)
    if completed_at > provider_as_of + _MAX_CAPTURE_DURATION:
        result = _partial_result(
            run_id=run_id,
            binding_id=binding_id,
            context=context,
            provider_as_of=provider_as_of,
            blocker="CAPTURE_DEADLINE_EXCEEDED",
            values=(first_digest, second_digest),
            completed_at=completed_at,
        )
    elif first_digest != second_digest:
        result = _partial_result(
            run_id=run_id,
            binding_id=binding_id,
            context=context,
            provider_as_of=provider_as_of,
            blocker="SNAPSHOT_DRIFT",
            values=(first_digest, second_digest),
            completed_at=completed_at,
        )
    else:
        result = BinanceUsdmReconciliationResult(
            run_id=run_id,
            binding_id=binding_id,
            account_id=context.account_id,
            provider_as_of=provider_as_of,
            started_at=provider_as_of,
            completed_at=completed_at,
            state="COMPLETE",
            stable=True,
            fact_digest=_fact_digest(provider_as_of, second_digest),
            blockers=(),
            capture=second,
        )
    await context.store.persist(result)
    return result


async def _capture_once(
    source: BinanceUsdmReconciliationSource,
    as_of: datetime,
) -> BinanceUsdmReconciliationCapture:
    capture = await source.capture(as_of)
    if type(capture) is not BinanceUsdmReconciliationCapture:
        raise TypeError("Binance USD-M reconciliation capture must be exact")
    capture.__post_init__()
    if capture.account.as_of != as_of:
        raise ValueError("Binance USD-M provider as-of differs")
    return capture


def _partial_result(
    *,
    run_id: UUID,
    binding_id: UUID,
    context: BinanceUsdmReconciliationContext,
    provider_as_of: datetime,
    blocker: str,
    values: tuple[bytes, ...],
    completed_at: datetime,
) -> BinanceUsdmReconciliationResult:
    digest = hashlib.sha256()
    digest.update(b"BINANCE_USDM_PARTIAL_V1")
    digest.update(blocker.encode("ascii"))
    for value in values:
        digest.update(value)
    return BinanceUsdmReconciliationResult(
        run_id=run_id,
        binding_id=binding_id,
        account_id=context.account_id,
        provider_as_of=provider_as_of,
        started_at=provider_as_of,
        completed_at=completed_at,
        state="PARTIAL",
        stable=False,
        fact_digest=digest.digest(),
        blockers=(blocker,),
        capture=None,
    )


def _projection_digest(capture: BinanceUsdmReconciliationCapture) -> bytes:
    account = capture.account
    configuration = capture.configuration
    payload = {
        "algoOrders": [
            {
                "clientId": value.client_algo_id,
                "closePosition": value.close_position,
                "id": value.algo_id,
                "quantity": _decimal_text(value.quantity),
                "side": value.side,
                "status": value.status,
                "symbol": value.symbol,
                "triggerPrice": _decimal_text(value.trigger_price),
                "type": value.order_type,
            }
            for value in sorted(account.algo_orders, key=lambda item: item.algo_id)
        ],
        "balances": [
            {
                "asset": value.asset,
                "available": _decimal_text(value.available_balance),
                "balance": _decimal_text(value.balance),
                "maximumWithdraw": _decimal_text(value.maximum_withdraw_amount),
                "updatedAt": value.updated_at.isoformat(),
            }
            for value in sorted(account.balances, key=lambda item: item.asset)
        ],
        "configuration": {
            "autoAddMargin": configuration.auto_add_margin,
            "leverage": configuration.leverage,
            "canTrade": configuration.can_trade,
            "marginType": configuration.margin_type,
            "maximumNotional": _decimal_text(configuration.maximum_notional),
            "minimumNotional": _decimal_text(configuration.minimum_notional),
            "positionMode": configuration.position_mode,
            "priceTick": _decimal_text(configuration.price_tick_size),
            "minimumQuantity": _decimal_text(configuration.minimum_quantity),
            "multiAssetsMargin": configuration.multi_assets_margin,
            "quantityStep": _decimal_text(configuration.quantity_step_size),
            "transferOutEnabled": configuration.transfer_out_enabled,
        },
        "income": [
            {
                "asset": value.asset,
                "income": _decimal_text(value.income),
                "tradeId": value.trade_id,
                "symbol": value.symbol,
                "time": value.occurred_at.isoformat(),
                "transactionId": value.transaction_id,
                "type": value.income_type,
            }
            for value in sorted(
                account.income,
                key=lambda item: item.transaction_id,
            )
        ],
        "normalOrders": [
            {
                "clientId": value.client_order_id,
                "closePosition": value.close_position,
                "executed": _decimal_text(value.executed_quantity),
                "id": value.order_id,
                "original": _decimal_text(value.original_quantity),
                "reduceOnly": value.reduce_only,
                "side": value.side,
                "status": value.status,
                "symbol": value.symbol,
                "type": value.order_type,
            }
            for value in sorted(account.normal_orders, key=lambda item: item.order_id)
        ],
        "positions": [
            {
                "amount": _decimal_text(value.amount),
                "entryPrice": _decimal_text(value.entry_price),
                "initialMargin": _decimal_text(value.initial_margin),
                "isolatedMargin": _decimal_text(value.isolated_margin),
                "maintenanceMargin": _decimal_text(value.maintenance_margin),
                "marginAsset": value.margin_asset,
                "markPrice": _decimal_text(value.mark_price),
                "notional": _decimal_text(value.notional),
                "openOrderInitialMargin": _decimal_text(
                    value.open_order_initial_margin
                ),
                "positionInitialMargin": _decimal_text(value.position_initial_margin),
                "positionSide": value.position_side,
                "symbol": value.symbol,
                "unrealizedPnl": _decimal_text(value.unrealized_pnl),
                "updatedAt": value.updated_at.isoformat(),
            }
            for value in sorted(
                account.positions,
                key=lambda item: (item.symbol, item.position_side),
            )
        ],
        "providerAsOf": account.as_of.isoformat(),
        "trades": [
            {
                "commission": _decimal_text(value.commission),
                "commissionAsset": value.commission_asset,
                "id": value.trade_id,
                "orderId": value.order_id,
                "price": _decimal_text(value.price),
                "quantity": _decimal_text(value.quantity),
                "realizedPnl": _decimal_text(value.realized_pnl),
                "side": value.side,
                "symbol": value.symbol,
                "time": value.occurred_at.isoformat(),
            }
            for value in sorted(account.trades, key=lambda item: item.trade_id)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


def _fact_digest(provider_as_of: datetime, projection_digest: bytes) -> bytes:
    digest = hashlib.sha256()
    digest.update(b"BINANCE_USDM_RECONCILIATION_FACT_V1")
    digest.update(provider_as_of.isoformat().encode("ascii"))
    digest.update(projection_digest)
    return digest.digest()


def _validate_account_scope(
    snapshot: BinanceUsdmAccountSnapshot,
    *,
    captured_at: datetime,
) -> None:
    for balance in snapshot.balances:
        balance.__post_init__()
        if balance.asset != "USDT" or balance.updated_at > captured_at:
            raise ValueError("Binance USD-M balance fact is invalid")
    position_identities = {
        (position.symbol, position.position_side) for position in snapshot.positions
    }
    if len(position_identities) != len(snapshot.positions):
        raise ValueError("duplicate Binance USD-M position identity")
    for position in snapshot.positions:
        position.__post_init__()
        if (
            position.symbol != _SYMBOL
            or position.position_side != "BOTH"
            or position.margin_asset != "USDT"
            or position.updated_at > captured_at
        ):
            raise ValueError("Binance USD-M position fact is invalid")
    _validate_normal_orders(snapshot.normal_orders)
    _validate_algo_orders(snapshot.algo_orders)
    _validate_trades(snapshot.trades, captured_at=captured_at)
    _validate_income(snapshot.income, captured_at=captured_at)


def _validate_normal_orders(values: tuple[BinanceUsdmOpenOrder, ...]) -> None:
    if len(values) != len({value.order_id for value in values}) or len(values) != len(
        {value.client_order_id for value in values}
    ):
        raise ValueError("duplicate Binance USD-M normal order identity")
    for value in values:
        _text(value.client_order_id, "normal client order ID", maximum=36)
        _text(value.status, "normal order status", maximum=32)
        _text(value.order_type, "normal order type", maximum=32)
        if (
            value.symbol != _SYMBOL
            or value.side not in {"BUY", "SELL"}
            or value.status not in _NORMAL_ORDER_STATUSES
            or type(value.order_id) is not int
            or value.order_id <= 0
            or _decimal(value.original_quantity, "normal original quantity") <= 0
            or _decimal(value.executed_quantity, "normal executed quantity") < 0
            or value.executed_quantity > value.original_quantity
            or type(value.reduce_only) is not bool
            or type(value.close_position) is not bool
        ):
            raise ValueError("Binance USD-M normal order fact is invalid")


def _validate_algo_orders(values: tuple[BinanceUsdmOpenAlgoOrder, ...]) -> None:
    if len(values) != len({value.algo_id for value in values}) or len(values) != len(
        {value.client_algo_id for value in values}
    ):
        raise ValueError("duplicate Binance USD-M algo order identity")
    for value in values:
        _text(value.client_algo_id, "algo client order ID", maximum=36)
        _text(value.status, "algo order status", maximum=32)
        _text(value.order_type, "algo order type", maximum=32)
        if (
            value.symbol != _SYMBOL
            or value.side not in {"BUY", "SELL"}
            or value.status not in _ALGO_ORDER_STATUSES
            or type(value.algo_id) is not int
            or value.algo_id <= 0
            or _decimal(value.quantity, "algo quantity") < 0
            or _decimal(value.trigger_price, "algo trigger price") <= 0
            or type(value.close_position) is not bool
        ):
            raise ValueError("Binance USD-M algo order fact is invalid")


def _validate_trades(
    values: tuple[BinanceUsdmTradeFact, ...],
    *,
    captured_at: datetime,
) -> None:
    if len(values) != len({value.trade_id for value in values}):
        raise ValueError("duplicate Binance USD-M trade identity")
    for value in values:
        _decimal(value.realized_pnl, "trade realized PnL")
        if (
            type(value.trade_id) is not int
            or value.trade_id <= 0
            or type(value.order_id) is not int
            or value.order_id <= 0
            or value.symbol != _SYMBOL
            or value.side not in {"BUY", "SELL"}
            or _decimal(value.quantity, "trade quantity") <= 0
            or _decimal(value.price, "trade price") <= 0
            or _decimal(value.commission, "trade commission") < 0
            or value.commission_asset != "USDT"
            or _utc(value.occurred_at, "trade occurred_at") > captured_at
        ):
            raise ValueError("Binance USD-M trade fact is invalid")


def _validate_income(
    values: tuple[BinanceUsdmIncomeFact, ...],
    *,
    captured_at: datetime,
) -> None:
    if len(values) != len({value.transaction_id for value in values}):
        raise ValueError("duplicate Binance USD-M income identity")
    for value in values:
        _text(value.trade_id, "income trade_id", maximum=64, empty=True)
        _text(value.income_type, "income type", maximum=32)
        _decimal(value.income, "income amount")
        if (
            type(value.transaction_id) is not int
            or value.transaction_id <= 0
            or value.symbol not in {"", _SYMBOL}
            or value.asset != "USDT"
            or _utc(value.occurred_at, "income occurred_at") > captured_at
        ):
            raise ValueError("Binance USD-M income fact is invalid")


def _capture_time(
    provider_as_of: datetime,
    clock: Callable[[], datetime],
) -> datetime:
    captured_at = _utc(clock(), "reconciliation clock")
    if captured_at <= provider_as_of:
        raise ValueError("Binance USD-M reconciliation time is invalid")
    return captured_at


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"Binance USD-M {name} must be an exact Decimal")
    return value


def _decimal_text(value: object) -> str:
    exact = _decimal(value, "canonical decimal")
    result = format(exact, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if result in {"", "-0"} else result


def _text(value: object, name: str, *, maximum: int, empty: bool = False) -> str:
    if (
        type(value) is not str
        or (not empty and not value)
        or len(value) > maximum
        or value.strip() != value
        or not value.isascii()
    ):
        raise ValueError(f"Binance USD-M {name} is invalid")
    return value


def _uuid7(value: object, name: str) -> UUID:
    if type(value) is not UUID or value.version != 7:
        raise ValueError(f"{name} must be UUIDv7")
    return value


def _utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is not UTC:
        raise ValueError(f"Binance USD-M {name} must use exact UTC")
    return value


__all__ = (
    "BinanceUsdmConfigurationFact",
    "BinanceUsdmReconciliationCapture",
    "BinanceUsdmReconciliationContext",
    "BinanceUsdmReconciliationResult",
    "BinanceUsdmReconciliationUnavailable",
    "MySqlBinanceUsdmReconciliationStore",
    "reconcile_binance_usdm",
)
