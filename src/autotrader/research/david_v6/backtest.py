from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import cast

from autotrader.domain.completed_ohlcv import CompletedOhlcvBar
from autotrader.domain.enums import Side
from autotrader.shared.decimal import require_decimal
from autotrader.strategies.david_v6.models import (
    EvidenceState,
    SetupGrade,
    V6Decision,
    canonical_v6_hash,
)


@dataclass(frozen=True, slots=True)
class BacktestFill:
    filled_at: datetime
    price: Decimal
    close_price: Decimal
    source_digest: bytes


@dataclass(frozen=True, slots=True)
class NoFill:
    expected_at: datetime
    reason_code: str = "MISSING_EXACT_NEXT_BAR"


@dataclass(frozen=True, slots=True)
class CostModel:
    timeframe: timedelta
    round_trip_cost_per_unit: Decimal | None
    cost_source_digest: bytes | None
    bar_source_digest: bytes

    def __post_init__(self) -> None:
        _require_timeframe(self.timeframe)
        _require_digest(self.bar_source_digest, "bar_source_digest")
        if self.round_trip_cost_per_unit is None:
            if self.cost_source_digest is not None:
                raise ValueError("unsupported cost cannot carry a source digest")
            return
        cost = require_decimal(self.round_trip_cost_per_unit)
        if cost < 0:
            raise ValueError("round_trip_cost_per_unit must be non-negative")
        if self.cost_source_digest is None:
            raise ValueError("supported cost requires a source digest")
        _require_digest(self.cost_source_digest, "cost_source_digest")
        object.__setattr__(self, "round_trip_cost_per_unit", cost)


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    train_started_at: datetime
    train_ended_at: datetime
    test_started_at: datetime
    test_ended_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "train_started_at",
            "train_ended_at",
            "test_started_at",
            "test_ended_at",
        ):
            object.__setattr__(
                self,
                name,
                _require_datetime(getattr(self, name), name),
            )
        if self.train_started_at >= self.train_ended_at:
            raise ValueError("walk-forward training window must be positive")
        if self.test_started_at >= self.test_ended_at:
            raise ValueError("walk-forward test window must be positive")
        if self.train_ended_at > self.test_started_at:
            raise ValueError("walk-forward train and test windows cannot overlap")


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    window: WalkForwardWindow
    state: EvidenceState
    sample_count: int
    gross_pnl: Decimal | None
    total_cost: Decimal | None
    net_pnl: Decimal | None
    unsupported_reason_codes: tuple[str, ...]
    source_digest: bytes


@dataclass(frozen=True, slots=True)
class WalkForwardPromotionEvidence:
    state: EvidenceState
    windows: tuple[WalkForwardResult, ...]
    sample_count: int
    gross_pnl: Decimal | None
    total_cost: Decimal | None
    net_pnl: Decimal | None
    positive_window_fraction: Decimal | None
    profit_factor: Decimal | None
    unsupported_reason_codes: tuple[str, ...]
    source_digests: tuple[bytes, ...]
    artifact_digest: bytes
    captured_at: datetime


def exact_next_bar_fill(
    *,
    signal_at: datetime,
    timeframe: timedelta,
    bars: Sequence[CompletedOhlcvBar],
) -> BacktestFill | NoFill:
    signal = _require_datetime(signal_at, "signal_at")
    interval = _require_timeframe(timeframe)
    canonical = _bars(bars)
    expected = signal + interval
    bar = next((item for item in canonical if item.timestamp == expected), None)
    if bar is None:
        return NoFill(expected_at=expected)
    return BacktestFill(
        filled_at=bar.timestamp,
        price=bar.open,
        close_price=bar.close,
        source_digest=canonical_v6_hash(bar),
    )


def run_walk_forward(
    *,
    decisions: Sequence[V6Decision],
    bars: Sequence[CompletedOhlcvBar],
    windows: Sequence[WalkForwardWindow],
    cost_model: CostModel,
) -> WalkForwardPromotionEvidence:
    canonical_decisions = _decisions(decisions)
    canonical_bars = _bars(bars)
    canonical_windows = _windows(windows)
    if type(cost_model) is not CostModel:
        raise TypeError("cost_model must be an exact CostModel")
    cost_model.__post_init__()
    captured_at = (
        canonical_windows[-1].test_ended_at
        if canonical_windows
        else datetime.min.replace(tzinfo=UTC)
    )
    source_digests = _source_digests(canonical_decisions, cost_model)
    if not canonical_windows:
        return _unsupported_evidence(
            state=EvidenceState.NOT_APPLICABLE,
            windows=(),
            reasons=("NO_WALK_FORWARD_WINDOWS",),
            source_digests=source_digests,
            captured_at=captured_at,
        )
    if cost_model.round_trip_cost_per_unit is None:
        return _unsupported_evidence(
            state=EvidenceState.NOT_APPLICABLE,
            windows=tuple(
                _unsupported_result(
                    window,
                    EvidenceState.NOT_APPLICABLE,
                    "UNSUPPORTED_COST_MODEL",
                )
                for window in canonical_windows
            ),
            reasons=("UNSUPPORTED_COST_MODEL",),
            source_digests=source_digests,
            captured_at=captured_at,
        )
    if not canonical_bars:
        return _unsupported_evidence(
            state=EvidenceState.NOT_APPLICABLE,
            windows=tuple(
                _unsupported_result(
                    window,
                    EvidenceState.NOT_APPLICABLE,
                    "UNSUPPORTED_BAR_DATA",
                )
                for window in canonical_windows
            ),
            reasons=("UNSUPPORTED_BAR_DATA",),
            source_digests=source_digests,
            captured_at=captured_at,
        )

    results = tuple(
        _run_window(
            window=window,
            decisions=canonical_decisions,
            bars=canonical_bars,
            cost_model=cost_model,
        )
        for window in canonical_windows
    )
    reasons = tuple(
        sorted(
            {reason for result in results for reason in result.unsupported_reason_codes}
        )
    )
    if any(result.state is EvidenceState.UNKNOWN for result in results):
        state = EvidenceState.UNKNOWN
    elif any(result.state is not EvidenceState.AVAILABLE for result in results):
        state = EvidenceState.NOT_APPLICABLE
    else:
        state = EvidenceState.AVAILABLE
    if state is not EvidenceState.AVAILABLE:
        return _unsupported_evidence(
            state=state,
            windows=results,
            reasons=reasons,
            source_digests=source_digests,
            captured_at=captured_at,
        )

    gross = sum(
        (cast(Decimal, result.gross_pnl) for result in results),
        start=Decimal(0),
    )
    costs = sum(
        (cast(Decimal, result.total_cost) for result in results),
        start=Decimal(0),
    )
    net = gross - costs
    positive_fraction = Decimal(
        sum(cast(Decimal, result.net_pnl) > 0 for result in results)
    ) / Decimal(len(results))
    window_net = tuple(cast(Decimal, result.net_pnl) for result in results)
    positive = sum((value for value in window_net if value > 0), start=Decimal(0))
    negative = abs(sum((value for value in window_net if value < 0), start=Decimal(0)))
    profit_factor = positive / negative if negative > 0 else None
    artifact = canonical_v6_hash(
        (
            EvidenceState.AVAILABLE,
            results,
            source_digests,
            captured_at,
        )
    )
    return WalkForwardPromotionEvidence(
        state=EvidenceState.AVAILABLE,
        windows=results,
        sample_count=sum(result.sample_count for result in results),
        gross_pnl=gross,
        total_cost=costs,
        net_pnl=net,
        positive_window_fraction=positive_fraction,
        profit_factor=profit_factor,
        unsupported_reason_codes=(),
        source_digests=source_digests,
        artifact_digest=artifact,
        captured_at=captured_at,
    )


def _run_window(
    *,
    window: WalkForwardWindow,
    decisions: tuple[V6Decision, ...],
    bars: tuple[CompletedOhlcvBar, ...],
    cost_model: CostModel,
) -> WalkForwardResult:
    selected = tuple(
        decision
        for decision in decisions
        if decision.grade is not SetupGrade.REJECT
        and window.test_started_at <= decision.generated_at < window.test_ended_at
        and decision.generated_at + cost_model.timeframe <= window.test_ended_at
    )
    if not selected:
        return _unsupported_result(
            window,
            EvidenceState.NOT_APPLICABLE,
            "NO_TEST_DECISIONS",
        )
    fills = tuple(
        (
            decision,
            exact_next_bar_fill(
                signal_at=decision.generated_at,
                timeframe=cost_model.timeframe,
                bars=bars,
            ),
        )
        for decision in selected
    )
    if any(isinstance(fill, NoFill) for _, fill in fills):
        return _unsupported_result(
            window,
            EvidenceState.UNKNOWN,
            "MISSING_EXACT_NEXT_BAR",
        )
    typed_fills = cast(tuple[tuple[V6Decision, BacktestFill], ...], fills)
    cost_per_unit = cast(Decimal, cost_model.round_trip_cost_per_unit)
    gross_values = tuple(_gross_pnl(decision, fill) for decision, fill in typed_fills)
    cost_values = tuple(
        cost_per_unit * decision.calculated_quantity for decision, _ in typed_fills
    )
    gross = sum(gross_values, start=Decimal(0))
    costs = sum(cost_values, start=Decimal(0))
    digest = canonical_v6_hash(
        (
            window,
            tuple(decision.decision_hash() for decision, _ in typed_fills),
            tuple(fill.source_digest for _, fill in typed_fills),
            cost_model.cost_source_digest,
            gross,
            costs,
        )
    )
    return WalkForwardResult(
        window=window,
        state=EvidenceState.AVAILABLE,
        sample_count=len(typed_fills),
        gross_pnl=gross,
        total_cost=costs,
        net_pnl=gross - costs,
        unsupported_reason_codes=(),
        source_digest=digest,
    )


def _gross_pnl(decision: V6Decision, fill: BacktestFill) -> Decimal:
    direction = Decimal(1) if decision.side is Side.BUY else Decimal(-1)
    return (fill.close_price - fill.price) * direction * decision.calculated_quantity


def _unsupported_result(
    window: WalkForwardWindow,
    state: EvidenceState,
    reason: str,
) -> WalkForwardResult:
    return WalkForwardResult(
        window=window,
        state=state,
        sample_count=0,
        gross_pnl=None,
        total_cost=None,
        net_pnl=None,
        unsupported_reason_codes=(reason,),
        source_digest=canonical_v6_hash((window, state, reason)),
    )


def _unsupported_evidence(
    *,
    state: EvidenceState,
    windows: tuple[WalkForwardResult, ...],
    reasons: tuple[str, ...],
    source_digests: tuple[bytes, ...],
    captured_at: datetime,
) -> WalkForwardPromotionEvidence:
    artifact = canonical_v6_hash((state, windows, reasons, source_digests, captured_at))
    return WalkForwardPromotionEvidence(
        state=state,
        windows=windows,
        sample_count=0,
        gross_pnl=None,
        total_cost=None,
        net_pnl=None,
        positive_window_fraction=None,
        profit_factor=None,
        unsupported_reason_codes=reasons,
        source_digests=source_digests,
        artifact_digest=artifact,
        captured_at=captured_at,
    )


def _bars(values: Sequence[CompletedOhlcvBar]) -> tuple[CompletedOhlcvBar, ...]:
    if any(type(value) is not CompletedOhlcvBar for value in values):
        raise TypeError("bars must contain exact CompletedOhlcvBar values")
    by_time: dict[datetime, CompletedOhlcvBar] = {}
    for bar in values:
        existing = by_time.get(bar.timestamp)
        if existing is not None and existing != bar:
            raise ValueError("completed bar timestamp payload collision")
        by_time[bar.timestamp] = bar
    return tuple(sorted(by_time.values(), key=lambda bar: bar.timestamp))


def _decisions(values: Sequence[V6Decision]) -> tuple[V6Decision, ...]:
    if any(type(value) is not V6Decision for value in values):
        raise TypeError("decisions must contain exact V6Decision values")
    by_id: dict[object, V6Decision] = {}
    for decision in values:
        existing = by_id.get(decision.id)
        if existing is not None and existing != decision:
            raise ValueError("v6 decision identity payload collision")
        by_id[decision.id] = decision
    return tuple(
        sorted(
            by_id.values(), key=lambda decision: (decision.generated_at, decision.id)
        )
    )


def _windows(values: Sequence[WalkForwardWindow]) -> tuple[WalkForwardWindow, ...]:
    if any(type(value) is not WalkForwardWindow for value in values):
        raise TypeError("windows must contain exact WalkForwardWindow values")
    canonical = tuple(sorted(values, key=lambda window: window.test_started_at))
    for previous, current in pairwise(canonical):
        if previous.test_ended_at > current.test_started_at:
            raise ValueError("walk-forward test windows cannot overlap")
    return canonical


def _source_digests(
    decisions: tuple[V6Decision, ...],
    cost_model: CostModel,
) -> tuple[bytes, ...]:
    values = {
        cost_model.bar_source_digest,
        *(
            digest
            for decision in decisions
            for digest in decision.source_evidence_hashes
        ),
    }
    if cost_model.cost_source_digest is not None:
        values.add(cost_model.cost_source_digest)
    return tuple(sorted(values))


def _require_timeframe(value: object) -> timedelta:
    if type(value) is not timedelta or value <= timedelta(0):
        raise ValueError("timeframe must be a positive timedelta")
    return value


def _require_datetime(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _require_digest(value: object, name: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        raise TypeError(f"{name} must be SHA-256 bytes")
    return value


__all__ = (
    "BacktestFill",
    "CostModel",
    "NoFill",
    "WalkForwardPromotionEvidence",
    "WalkForwardResult",
    "WalkForwardWindow",
    "exact_next_bar_fill",
    "run_walk_forward",
)
