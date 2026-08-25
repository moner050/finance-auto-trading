from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import Side
from autotrader.strategies.david_v6.exhaustion import evaluate_exhaustion
from autotrader.strategies.david_v6.pivots import Pivot, PivotKind
from autotrader.strategies.david_v6.zones import HlitZone, ZoneFacts

START = datetime(2026, 8, 24, tzinfo=UTC)


def _bars() -> tuple[CompletedOhlcvBar, ...]:
    lows = (12, 12, 12, 9, 11, 11, 11, 8, 10, 10, 10, 7, 6)
    highs = (20, 20, 20, 21, 20, 20, 20, 22, 20, 20, 20, 23, 24)
    volumes = (1, 1, 1, 30, 1, 1, 1, 20, 1, 1, 1, 10, 5)
    return tuple(
        CompletedOhlcvBar(
            timestamp=START + timedelta(minutes=index),
            open=Decimal(low),
            high=Decimal(high),
            low=Decimal(low),
            close=Decimal(low),
            volume=Decimal(volume),
        )
        for index, (low, high, volume) in enumerate(
            zip(lows, highs, volumes, strict=True)
        )
    )


def _zone_facts(bars: tuple[CompletedOhlcvBar, ...]) -> ZoneFacts:
    return ZoneFacts(
        observed_at=bars[-1].timestamp,
        source_timezone="UTC",
        selected_dates=(date(2026, 8, 24),),
        bin_count=1,
        zones=(
            HlitZone(
                lower_boundary=Decimal("0"),
                upper_boundary=Decimal("30"),
                touch_count=len(bars),
                strength=5,
                touched_at=tuple(bar.timestamp for bar in bars),
            ),
        ),
    )


def test_exhaustion_is_symmetric_and_ignores_unconfirmed_pivots() -> None:
    bars = _bars()
    pivots = (
        Pivot(3, 3, PivotKind.LOW, Decimal("9"), bars[3].timestamp, True),
        Pivot(7, 7, PivotKind.LOW, Decimal("8"), bars[7].timestamp, True),
        Pivot(11, 11, PivotKind.LOW, Decimal("7"), bars[11].timestamp, True),
        Pivot(12, 12, PivotKind.LOW, Decimal("6"), bars[12].timestamp, False),
        Pivot(3, 3, PivotKind.HIGH, Decimal("21"), bars[3].timestamp, True),
        Pivot(7, 7, PivotKind.HIGH, Decimal("22"), bars[7].timestamp, True),
        Pivot(11, 11, PivotKind.HIGH, Decimal("23"), bars[11].timestamp, True),
    )

    facts = evaluate_exhaustion(bars, zones=_zone_facts(bars), pivots=pivots)

    assert facts.bullish is not None
    assert facts.bearish is not None
    assert facts.bullish.direction is Side.BUY
    assert facts.bearish.direction is Side.SELL
    assert facts.bullish.structural_reference_price == Decimal("7")
    assert tuple(pivot.index for pivot in facts.bullish.history) == (3, 7, 11)
    assert facts.bullish.confirmed_at == bars[11].timestamp


def test_exhaustion_history_is_append_only_while_evaluation_rolls_last_four() -> None:
    bars = _bars()
    base = tuple(
        Pivot(index, index, PivotKind.LOW, Decimal(price), bars[index].timestamp, True)
        for index, price in ((3, 9), (7, 8), (11, 7))
    )
    first = evaluate_exhaustion(bars, zones=_zone_facts(bars), pivots=base)
    extended_bar = CompletedOhlcvBar(
        timestamp=bars[-1].timestamp + timedelta(minutes=1),
        open=Decimal("5"),
        high=Decimal("24"),
        low=Decimal("5"),
        close=Decimal("5"),
        volume=Decimal("4"),
    )
    extended_bars = (*bars, extended_bar)
    extended = evaluate_exhaustion(
        extended_bars,
        zones=_zone_facts(extended_bars),
        pivots=(
            *base,
            Pivot(13, 13, PivotKind.LOW, Decimal("5"), extended_bar.timestamp, True),
        ),
    )

    assert first.bullish is not None
    assert extended.bullish is not None
    assert extended.bullish.history[:3] == first.bullish.history
    assert len(extended.bullish.evaluation_pivots) == 4
