from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from typing import cast

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.strategies.david_v6.evidence import EvidenceItem, EvidenceProvenance
from autotrader.strategies.david_v6.models import EvidenceState, V6Market

_SMA_FAST = 6
_SMA_MID = 70
_SMA_SLOW = 200
_MACD_FAST = 12
_MACD_SLOW = 26
_MACD_SIGNAL = 9
_REQUIRED_DAILY_BARS = _SMA_SLOW + 1
# The first bar whose MACD signal exists: the slow EMA seeds at 26 and the
# signal EMA needs a further nine values.
MACD_WARMUP_BARS = _MACD_SLOW + _MACD_SIGNAL - 1


@dataclass(frozen=True, slots=True)
class MetodoFacts:
    observed_at: datetime
    sma_6: Decimal
    sma_70: Decimal
    sma_200: Decimal
    sma_70_slope: Decimal
    sma_200_slope: Decimal
    trend_up: bool
    trend_down: bool
    sma_6_70_cross_up: bool
    sma_6_70_cross_down: bool
    macd: Decimal
    macd_signal: Decimal
    macd_cross_up_above_zero: bool
    macd_cross_up_below_zero: bool
    latest_volume: Decimal
    mean_volume_20d: Decimal
    normal_technical_confirmation: bool
    same_bar_a_confirmation: bool


def evaluate_metodo(
    *,
    market: V6Market,
    daily_bars: Sequence[CompletedOhlcvBar],
    decision_at: datetime,
) -> EvidenceItem[MetodoFacts]:
    if type(cast(object, market)) is not V6Market:
        raise TypeError("market must be an exact V6Market")
    decision = _utc(decision_at)
    if market is V6Market.BINANCE_USDM:
        return EvidenceItem(
            state=EvidenceState.NOT_APPLICABLE,
            value=None,
            provenance=None,
            blocker_code="METODO_CASH_ONLY",
        )
    bars = _completed_before(daily_bars, decision)
    if bars is None or len(bars) < _REQUIRED_DAILY_BARS:
        return EvidenceItem(
            state=EvidenceState.UNAVAILABLE,
            value=None,
            provenance=None,
            blocker_code="METODO_DAILY_WARMUP_UNAVAILABLE",
        )
    closes = tuple(bar.close for bar in bars)
    current_sma_6 = _sma(closes, _SMA_FAST, 0)
    previous_sma_6 = _sma(closes, _SMA_FAST, 1)
    current_sma_70 = _sma(closes, _SMA_MID, 0)
    previous_sma_70 = _sma(closes, _SMA_MID, 1)
    current_sma_200 = _sma(closes, _SMA_SLOW, 0)
    previous_sma_200 = _sma(closes, _SMA_SLOW, 1)
    macd_values, signal_values = macd_series(closes)
    current_macd = cast(Decimal, macd_values[-1])
    previous_macd = cast(Decimal, macd_values[-2])
    current_signal = cast(Decimal, signal_values[-1])
    previous_signal = cast(Decimal, signal_values[-2])
    trend_up = (
        current_sma_200 > previous_sma_200
        and current_sma_70 > current_sma_200
        and current_sma_70 > previous_sma_70
    )
    trend_down = (
        current_sma_200 < previous_sma_200
        and current_sma_70 < current_sma_200
        and current_sma_70 < previous_sma_70
    )
    cross_up = previous_sma_6 <= previous_sma_70 and current_sma_6 > current_sma_70
    cross_down = previous_sma_6 >= previous_sma_70 and current_sma_6 < current_sma_70
    macd_crosses_up = previous_macd <= previous_signal and current_macd > current_signal
    macd_cross_up = macd_crosses_up and current_macd > 0
    # Section 2.3 signal C: a cross below zero is admissible only when the
    # regime reports a pessimism extreme, which this module cannot see.
    macd_cross_up_below_zero = macd_crosses_up and current_macd < 0
    facts = MetodoFacts(
        observed_at=bars[-1].timestamp,
        sma_6=current_sma_6,
        sma_70=current_sma_70,
        sma_200=current_sma_200,
        sma_70_slope=current_sma_70 - previous_sma_70,
        sma_200_slope=current_sma_200 - previous_sma_200,
        trend_up=trend_up,
        trend_down=trend_down,
        sma_6_70_cross_up=cross_up,
        sma_6_70_cross_down=cross_down,
        macd=current_macd,
        macd_signal=current_signal,
        macd_cross_up_above_zero=macd_cross_up,
        macd_cross_up_below_zero=macd_cross_up_below_zero,
        latest_volume=bars[-1].volume,
        mean_volume_20d=(
            sum((bar.volume for bar in bars[-20:]), start=Decimal(0)) / Decimal(20)
        ),
        normal_technical_confirmation=trend_up and (cross_up or macd_cross_up),
        same_bar_a_confirmation=trend_up and cross_up and macd_cross_up,
    )
    digest = _bars_digest(bars)
    return EvidenceItem(
        state=EvidenceState.AVAILABLE,
        value=facts,
        provenance=EvidenceProvenance(
            source="DAVID_V6_DERIVED",
            source_key=f"metodo:daily:{digest}",
            source_timezone="UTC",
            observed_at=bars[-1].timestamp,
            captured_at=decision,
            digest_sha256=digest,
        ),
        blocker_code=None,
    )


def _completed_before(
    values: Sequence[CompletedOhlcvBar], decision_at: datetime
) -> tuple[CompletedOhlcvBar, ...] | None:
    bars = tuple(values)
    if any(type(bar) is not CompletedOhlcvBar for bar in bars):
        return None
    completed = tuple(bar for bar in bars if bar.timestamp < decision_at)
    if any(
        later.timestamp <= earlier.timestamp for earlier, later in pairwise(completed)
    ):
        return None
    return completed


def _sma(closes: tuple[Decimal, ...], period: int, offset: int) -> Decimal:
    end = len(closes) - offset
    return sum(closes[end - period : end], start=Decimal(0)) / Decimal(period)


def macd_series(
    closes: tuple[Decimal, ...],
) -> tuple[tuple[Decimal | None, ...], tuple[Decimal | None, ...]]:
    """MACD 12/26/9, shared by the daily swing and by HLIT (section 12)."""
    fast = _ema(closes, _MACD_FAST)
    slow = _ema(closes, _MACD_SLOW)
    macd: list[Decimal | None] = [None] * len(closes)
    for index, (fast_value, slow_value) in enumerate(zip(fast, slow, strict=True)):
        if fast_value is not None and slow_value is not None:
            macd[index] = fast_value - slow_value
    signal: list[Decimal | None] = [None] * len(closes)
    first_macd = _MACD_SLOW - 1
    first_signal = first_macd + _MACD_SIGNAL - 1
    seed = sum(
        (cast(Decimal, macd[index]) for index in range(first_macd, first_signal + 1)),
        start=Decimal(0),
    ) / Decimal(_MACD_SIGNAL)
    signal[first_signal] = seed
    multiplier = Decimal(2) / Decimal(_MACD_SIGNAL + 1)
    for index in range(first_signal + 1, len(closes)):
        current = cast(Decimal, macd[index])
        seed += multiplier * (current - seed)
        signal[index] = seed
    return tuple(macd), tuple(signal)


def _ema(closes: tuple[Decimal, ...], period: int) -> tuple[Decimal | None, ...]:
    values: list[Decimal | None] = [None] * len(closes)
    seed = sum(closes[:period], start=Decimal(0)) / Decimal(period)
    values[period - 1] = seed
    multiplier = Decimal(2) / Decimal(period + 1)
    for index in range(period, len(closes)):
        seed += multiplier * (closes[index] - seed)
        values[index] = seed
    return tuple(values)


def _bars_digest(bars: tuple[CompletedOhlcvBar, ...]) -> str:
    payload = tuple(
        {
            "timestamp": bar.timestamp.isoformat(),
            "open": _decimal(bar.open),
            "high": _decimal(bar.high),
            "low": _decimal(bar.low),
            "close": _decimal(bar.close),
            "volume": _decimal(bar.volume),
        }
        for bar in bars
    )
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _decimal(value: Decimal) -> str:
    return "0" if value.is_zero() else format(value.normalize(), "f")


def _utc(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("decision_at must be timezone-aware")
    return value.astimezone(UTC)


__all__ = ("MACD_WARMUP_BARS", "MetodoFacts", "evaluate_metodo", "macd_series")
