"""What an open position's world looks like when a measurement is missing.

The manager acts on these fields. Every one of them can be absent, and the
question each test answers is what absence means - because the wrong answer
here does not refuse a trade, it closes one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from autotrader.domain.enums import Side
from autotrader.strategies.david_v6.metodo import MetodoFacts
from autotrader.strategies.david_v6.models import EvidenceState
from autotrader.strategies.david_v6.position_facts import position_facts
from autotrader.strategies.david_v6.sessions import SessionFacts

PRICE = Decimal("100")


class _Item:
    """An evidence slot, present or not."""

    def __init__(self, value: object = None, *, available: bool = True) -> None:
        self.state = EvidenceState.AVAILABLE if available else EvidenceState.UNAVAILABLE
        self.value = value


class _Bundle:
    def __init__(
        self,
        *,
        order_flow: _Item,
        metodo: _Item,
        session: _Item | None = None,
    ) -> None:
        self.order_flow = order_flow
        self.metodo = metodo
        self.session = session or _Item(available=False)


class _Fees:
    def __init__(self, entry: str | None = "0.01", exit_: str | None = "0.02") -> None:
        self.entry_fee_per_unit = None if entry is None else Decimal(entry)
        self.exit_taker_fee_per_unit = None if exit_ is None else Decimal(exit_)


def _metodo(*, cross_down: bool = False, cross_up: bool = False) -> MetodoFacts:
    return MetodoFacts(
        observed_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        sma_6=Decimal("100"),
        sma_70=Decimal("100"),
        sma_200=Decimal("100"),
        sma_70_slope=Decimal("0"),
        sma_200_slope=Decimal("0"),
        trend_up=False,
        trend_down=False,
        sma_6_70_cross_up=cross_up,
        sma_6_70_cross_down=cross_down,
        macd=Decimal("0"),
        macd_signal=Decimal("0"),
        macd_cross_up_above_zero=False,
        macd_cross_up_below_zero=False,
        latest_volume=Decimal("100"),
        mean_volume_20d=Decimal("100"),
        normal_technical_confirmation=False,
        same_bar_a_confirmation=False,
    )


def _facts(**changes: object):
    values: dict[str, object] = {
        "side": Side.BUY,
        "setup": None,
        "current_price": PRICE,
        "atr_5m": Decimal("2"),
        "tick_size": Decimal("0.1"),
        "fee_schedule": _Fees(),
        "stop_slippage_q95": Decimal("3"),
        "protection_failed": False,
    }
    bundle = changes.pop("bundle", None) or _Bundle(
        order_flow=_Item(available=False), metodo=_Item(available=False)
    )
    values.update(changes)
    return position_facts(bundle, **values)  # type: ignore[arg-type]


def test_an_unavailable_order_flow_is_not_a_blocking_big_trade() -> None:
    """Closing a position because a measurement failed acts on evidence that
    was never taken, and the structural stop is behind it either way."""
    assert _facts().blocking_big_trade is False


def test_an_unavailable_metodo_is_not_an_exit_signal() -> None:
    assert _facts().metodo_exit_signal is False


def test_a_long_exits_on_the_cross_down() -> None:
    bundle = _Bundle(
        order_flow=_Item(available=False),
        metodo=_Item(_metodo(cross_down=True)),
    )

    assert _facts(bundle=bundle, side=Side.BUY).metodo_exit_signal is True
    assert _facts(bundle=bundle, side=Side.SELL).metodo_exit_signal is False


def test_a_short_exits_on_the_cross_up() -> None:
    """Reading only the down-cross would leave every short holding through
    its own exit signal - the same one-sided wiring that made every
    evaluation a BUY."""
    bundle = _Bundle(
        order_flow=_Item(available=False),
        metodo=_Item(_metodo(cross_up=True)),
    )

    assert _facts(bundle=bundle, side=Side.SELL).metodo_exit_signal is True
    assert _facts(bundle=bundle, side=Side.BUY).metodo_exit_signal is False


def test_an_unranked_slippage_sample_is_declared_insufficient() -> None:
    """Rather than handed over as a number. A stop distance modelled on too
    few bars is a figure that looks measured and is not."""
    facts = _facts(stop_slippage_q95=None)

    assert facts.slippage_sample_sufficient is False
    assert facts.q95_adverse_stop_slippage == Decimal(0)


def test_a_ranked_slippage_sample_is_passed_through() -> None:
    facts = _facts(stop_slippage_q95=Decimal("3"))

    assert facts.slippage_sample_sufficient is True
    assert facts.q95_adverse_stop_slippage == Decimal("3")


def test_no_setup_means_no_levels_to_take_profit_at() -> None:
    facts = _facts(setup=None)

    assert facts.fib_25_price is None
    assert facts.fib_50_price is None
    assert facts.fib_66_price is None


def test_a_missing_fee_reads_as_zero_rather_than_refusing() -> None:
    """The fee only widens the break-even, so an absent one makes the manager
    move a stop slightly early rather than not at all."""
    facts = _facts(fee_schedule=_Fees(entry=None, exit_=None))

    assert facts.actual_entry_fee_per_unit == Decimal(0)
    assert facts.taker_exit_fee_per_unit == Decimal(0)


def test_protection_failure_is_carried_through_untouched() -> None:
    assert _facts(protection_failed=True).protection_failed is True


def _session(*, flat: bool) -> object:
    return SessionFacts(
        state=EvidenceState.AVAILABLE,
        session_open=True,
        entry_allowed=not flat,
        reduce_only=flat,
        must_be_flat=flat,
        overnight_allowed=False,
        pre_open=False,
        size_multiplier=Decimal(1),
        max_micro_contracts=None,
        entry_cutoff_at=datetime(2026, 9, 2, 19, 30, tzinfo=UTC),
        flat_at=datetime(2026, 9, 2, 19, 50, tzinfo=UTC),
        blockers=(),
    )


def test_the_session_flat_deadline_reaches_the_manager() -> None:
    """Section 7 wants the book flat before the close and forbids overnight.
    The session evaluation already computed this and it was only ever read as
    a blocker on a new entry, which does nothing about what is already held."""
    bundle = _Bundle(
        order_flow=_Item(available=False),
        metodo=_Item(available=False),
        session=_Item(_session(flat=True)),
    )

    assert _facts(bundle=bundle).must_be_flat is True


def test_an_open_session_does_not_ask_for_a_flat_book() -> None:
    bundle = _Bundle(
        order_flow=_Item(available=False),
        metodo=_Item(available=False),
        session=_Item(_session(flat=False)),
    )

    assert _facts(bundle=bundle).must_be_flat is False


def test_an_unreadable_clock_is_not_a_reason_to_close() -> None:
    """Nor a reason to hold. It resolves like every other absence here: the
    position keeps the stop it already has, and the entry path independently
    refuses to open while the session evidence is missing, so nothing new
    accumulates behind a deadline nobody can see."""
    assert _facts().must_be_flat is False
