from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import OrderStyle, Side
from autotrader.integrations.brokers.internal_paper import (
    InternalPaperBroker,
    PaperExecutionBar,
    PaperOrderCommand,
    PaperOrderReceipt,
    PaperOrderStatus,
)
from autotrader.shared.ids import new_uuid7
from autotrader.strategies.david_v6.models import V6Market

NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)


class _MarketData:
    def __init__(self, execution_bar: PaperExecutionBar | None) -> None:
        self.execution_bar = execution_bar
        self.calls = 0

    async def next_bar(
        self, command: PaperOrderCommand, *, now: datetime
    ) -> PaperExecutionBar | None:
        del now
        del command
        self.calls += 1
        return self.execution_bar


class _Journal:
    def __init__(self) -> None:
        self.commands: dict[object, bytes] = {}
        self.receipts: dict[object, PaperOrderReceipt] = {}
        self.staged = 0
        self.persisted = 0

    async def load_receipt(self, command_id: object) -> PaperOrderReceipt | None:
        return self.receipts.get(command_id)

    async def stage_command(self, command: PaperOrderCommand, digest: bytes) -> None:
        existing = self.commands.get(command.id)
        if existing is not None and existing != digest:
            raise ValueError("paper command identity payload collision")
        if existing is None:
            self.commands[command.id] = digest
            self.staged += 1

    async def persist_receipt(self, receipt: PaperOrderReceipt) -> None:
        self.receipts[receipt.command_id] = receipt
        self.persisted += 1

    async def latest_receipt_for_order(
        self, order_id: object
    ) -> PaperOrderReceipt | None:
        return next(
            (
                receipt
                for receipt in self.receipts.values()
                if receipt.order_id == order_id
            ),
            None,
        )


def _bar(
    *,
    timestamp: datetime | None = None,
    available_quantity: Decimal = Decimal("10"),
) -> PaperExecutionBar:
    return PaperExecutionBar(
        bar=CompletedOhlcvBar(
            timestamp=timestamp or NOW + timedelta(minutes=1),
            open=Decimal("101"),
            high=Decimal("103"),
            low=Decimal("99"),
            close=Decimal("102"),
            volume=Decimal("1000"),
        ),
        available_quantity=available_quantity,
        source_digest=b"b" * 32,
    )


def _command(**changes: object) -> PaperOrderCommand:
    command = PaperOrderCommand(
        id=new_uuid7(),
        order_id=new_uuid7(),
        account_alias="internal-binance-usdm-paper",
        market=V6Market.BINANCE_USDM,
        side=Side.BUY,
        order_style=OrderStyle.MARKET,
        quantity=Decimal("2"),
        limit_price=None,
        signal_at=NOW,
        timeframe=timedelta(minutes=1),
        fee_per_unit=Decimal("0.2"),
        slippage_per_unit=Decimal("0.1"),
    )
    values = {field: getattr(command, field) for field in command.__dataclass_fields__}
    values.update(changes)
    return PaperOrderCommand(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("alias", "market"),
    (
        ("internal-us-paper", V6Market.US_CASH),
        ("internal-binance-usdm-paper", V6Market.BINANCE_USDM),
    ),
)
def test_only_exact_internal_paper_account_market_bindings_are_accepted(
    alias: str,
    market: V6Market,
) -> None:
    assert _command(account_alias=alias, market=market).account_alias == alias

    with pytest.raises(ValueError, match="account binding"):
        _command(account_alias=alias, market=V6Market.KRX_CASH)


@pytest.mark.asyncio
async def test_exact_next_bar_fill_applies_fees_and_adverse_slippage() -> None:
    journal = _Journal()
    broker = InternalPaperBroker(journal=journal, market_data=_MarketData(_bar()))
    command = _command()

    receipt = await broker.submit(command, now=NOW)
    state = await broker.reconcile(command.order_id)

    assert receipt.status is PaperOrderStatus.FILLED
    assert receipt.filled_quantity == Decimal("2")
    assert receipt.fill_price == Decimal("101.1")
    assert receipt.fee == Decimal("0.4")
    assert receipt.slippage_cost == Decimal("0.2")
    assert state.status is PaperOrderStatus.FILLED
    assert journal.staged == journal.persisted == 1


@pytest.mark.asyncio
async def test_later_bar_is_no_fill_and_is_never_substituted() -> None:
    journal = _Journal()
    broker = InternalPaperBroker(
        journal=journal,
        market_data=_MarketData(_bar(timestamp=NOW + timedelta(minutes=2))),
    )

    receipt = await broker.submit(_command(), now=NOW)

    assert receipt.status is PaperOrderStatus.NO_FILL
    assert receipt.reason_code == "MISSING_EXACT_NEXT_BAR"
    assert receipt.filled_quantity == 0


@pytest.mark.asyncio
async def test_partial_fill_uses_only_available_next_bar_liquidity() -> None:
    broker = InternalPaperBroker(
        journal=_Journal(),
        market_data=_MarketData(_bar(available_quantity=Decimal("0.75"))),
    )

    receipt = await broker.submit(_command(quantity=Decimal("2")), now=NOW)

    assert receipt.status is PaperOrderStatus.PARTIALLY_FILLED
    assert receipt.filled_quantity == Decimal("0.75")
    assert receipt.remaining_quantity == Decimal("1.25")


@pytest.mark.asyncio
async def test_duplicate_command_id_reuses_one_persisted_receipt() -> None:
    journal = _Journal()
    market_data = _MarketData(_bar())
    broker = InternalPaperBroker(journal=journal, market_data=market_data)
    command = _command()

    first = await broker.submit(command, now=NOW)
    second = await broker.submit(command, now=NOW)

    assert second == first
    assert market_data.calls == 1
    assert journal.staged == journal.persisted == 1


@pytest.mark.asyncio
async def test_duplicate_command_id_with_changed_payload_is_rejected() -> None:
    journal = _Journal()
    broker = InternalPaperBroker(journal=journal, market_data=_MarketData(_bar()))
    command = _command()
    await broker.submit(command, now=NOW)

    with pytest.raises(ValueError, match="identity payload collision"):
        await broker.submit(_command(id=command.id, quantity=Decimal("3")), now=NOW)


def test_internal_adapter_exposes_no_live_provider_transport() -> None:
    broker = InternalPaperBroker(journal=_Journal(), market_data=_MarketData(_bar()))

    assert not hasattr(broker, "transport")
    assert not hasattr(broker, "provider_client")


@pytest.mark.asyncio
async def test_a_stop_fills_at_its_trigger_when_the_bar_crossed_it_midway() -> None:
    """The bar opened at 101 and fell to 99, so a stop at 100 was reached
    partway through. Filling at the open would report 101, a price the market
    never offered to a seller once the stop had triggered."""
    journal = _Journal()
    broker = InternalPaperBroker(journal=journal, market_data=_MarketData(_bar()))

    receipt = await broker.submit(
        _command(side=Side.SELL, trigger_price=Decimal("100")), now=NOW
    )

    assert receipt.status is PaperOrderStatus.FILLED
    assert receipt.fill_price == Decimal("100") - Decimal("0.1")


@pytest.mark.asyncio
async def test_a_stop_fills_at_the_open_when_the_bar_gapped_past_it() -> None:
    """A gap does not hand the seller the stop price. The open is the first
    price anyone could actually have traded at."""
    journal = _Journal()
    broker = InternalPaperBroker(journal=journal, market_data=_MarketData(_bar()))

    receipt = await broker.submit(
        # The bar opens at 101, already below a stop of 110.
        _command(side=Side.SELL, trigger_price=Decimal("110")),
        now=NOW,
    )

    assert receipt.fill_price == Decimal("101") - Decimal("0.1")


@pytest.mark.asyncio
async def test_a_stop_is_resolved_by_a_later_bar_unlike_a_one_shot_order() -> None:
    journal = _Journal()
    late = _bar(timestamp=NOW + timedelta(minutes=5))
    broker = InternalPaperBroker(journal=journal, market_data=_MarketData(late))

    resting = await broker.submit(
        _command(side=Side.SELL, trigger_price=Decimal("100")), now=NOW
    )
    one_shot = await broker.submit(_command(side=Side.SELL), now=NOW)

    # A stop waits for whichever bar reaches it.
    assert resting.status is PaperOrderStatus.FILLED
    # A one-shot order is settled by its own bar and by no other.
    assert one_shot.status is PaperOrderStatus.NO_FILL
    assert one_shot.reason_code == "MISSING_EXACT_NEXT_BAR"


def test_a_stop_limit_is_refused_rather_than_half_modelled() -> None:
    with pytest.raises(ValueError, match="triggered order must be a market order"):
        _command(
            order_style=OrderStyle.LIMIT,
            limit_price=Decimal("100"),
            trigger_price=Decimal("99"),
        )
