"""Every weighted indicator either has a producer or is named as missing.

F14: six of the twelve weights in §21.3 had nothing that could ever emit them.
Four of those were not unimplemented - `order_flow.py` computed secado and the
reversal MIG on every pass and carried them on `OrderFlowFacts`, and the
assembly read only `big_trades` off that object. Measurement present, weight
present, wire absent.

The consequence was arithmetic rather than rare: the reachable maximum was +5
against a candidate threshold of 7, so `A_CANDIDATE` and `A` could not be
graded at all and any entry would have been sized at the normal fraction.

This reads the assembly's source for the keys it can emit and compares that
with the weight table. It fails when a producer is removed, and it fails when
a weight is added with nothing behind it.
"""

from __future__ import annotations

import re
from pathlib import Path

from autotrader.strategies.david_v6.grading import (
    _RESEARCH_WEIGHTS,
    ABNORMAL_SPREAD_OR_SLIPPAGE,
    CANDIDATE_SCORE,
    FIBONACCI_EXTENSION_CLUSTER,
    V1_CERO_OSMOTICO,
    indicator_weight,
)

ASSEMBLY = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "autotrader"
    / "strategies"
    / "david_v6"
    / "assembly.py"
)

# Named rather than skipped. Each is absent for a stated reason, and a reader
# who implements one has to come here to say so.
#
# The first two have no calculator anywhere - they are unimplemented, not
# unwired, and they are the two largest remaining sources of score. The third
# has no calculator either. `v1_cero_osmotico` is `telemetry_only` at LOW
# confidence in §15.2 and `_TELEMETRY_ONLY` already forces its weight to zero,
# so producing it would add a row that cannot matter.
WITHOUT_A_PRODUCER = frozenset(
    {
        ABNORMAL_SPREAD_OR_SLIPPAGE,
        FIBONACCI_EXTENSION_CLUSTER,
        V1_CERO_OSMOTICO,
    }
)

_EMITTED = re.compile(r"_indicator\(\s*([A-Z][A-Z0-9_]*)\s*,")


def _producible() -> set[str]:
    """The weight-table keys the assembly can actually emit."""
    source = ASSEMBLY.read_text(encoding="utf-8")
    constants = set(_EMITTED.findall(source))
    lookup = {
        name: value
        for name, value in vars(
            __import__("autotrader.strategies.david_v6.grading", fromlist=["grading"])
        ).items()
        if isinstance(value, str)
    }
    return {lookup[name] for name in constants if name in lookup}


def test_the_scan_finds_the_producers() -> None:
    """A regex that matched nothing would make every assertion below vacuous."""
    producible = _producible()

    assert len(producible) >= 6
    assert "regular_hlit_divergence" in producible
    assert "v1_secado" in producible, "wired by F14"
    assert "higher_timeframe_bias_aligned" in producible, "wired by F14"


def test_every_weight_has_a_producer_or_is_named() -> None:
    missing = set(_RESEARCH_WEIGHTS) - _producible()

    assert missing == WITHOUT_A_PRODUCER


def test_the_candidate_grade_is_reachable() -> None:
    """What F14 was about. Blocking and supporting big trades are exclusive,
    so the ceiling counts the better of the two."""
    producible = _producible()
    positive = {
        key: indicator_weight(key) for key in producible if indicator_weight(key) > 0
    }
    ceiling = sum(positive.values())

    assert ceiling >= CANDIDATE_SCORE, (
        f"reachable maximum is {ceiling} against a threshold of "
        f"{CANDIDATE_SCORE}: {sorted(positive)}"
    )
