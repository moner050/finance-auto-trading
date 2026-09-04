"""Whether a LIVE run may start, and what it says when it may not.

§29.8: no promotion gate has been passed. So this refuses today, and the
tests that matter are that it refuses for the right reasons, names all of
them at once, and would stop refusing if the state changed - a gate that
could never open is a wall, and a wall teaches nobody what is missing.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from uuid import uuid7

import pytest

from autotrader.apps.backoffice.promotion_read_model import (
    BindingProgress,
    PromotionView,
)
from autotrader.apps.trader.live_gate import (
    ACCOUNT_NOT_LIVE,
    ALLOW_LIVE_ABSENT,
    BINDING_ABSENT,
    MODE_NOT_LIVE,
    NO_COMPOSITION,
    NOT_PROMOTED,
    may_start_live,
)
from autotrader.config.settings import RuntimeMode, Settings

TODAY = date(2026, 9, 4)
BINDING = uuid7()
ACCOUNT = uuid7()


def _settings(**changes: object) -> Settings:
    return Settings(
        allow_live=bool(changes.get("allow_live", True)),
        trading_mode=changes.get("trading_mode", RuntimeMode.LIVE),  # pyright: ignore[reportArgumentType]
    )


def _promotion(*, ready: bool = True, binding_id: object = BINDING) -> PromotionView:
    return PromotionView(
        required=2,
        bindings=(
            BindingProgress(
                binding_id=binding_id,  # pyright: ignore[reportArgumentType]
                account_id=ACCOUNT,
                account_alias="live",
                provider_code="BINANCE",
                environment="LIVE",
                manifest_id=uuid7(),
                manifest_version="v6.0",
                shadow_dates=(),
                paper_dates=(),
                shadow_remaining=0 if ready else 2,
                paper_remaining=0 if ready else 2,
                ready=ready,
                sessions=(),
            ),
        ),
        modes=("SHADOW", "PAPER"),
        manifest_missing=False,
    )


def _decide(**changes: object):
    return may_start_live(
        settings=changes.get("settings", _settings()),  # pyright: ignore[reportArgumentType]
        account_environment=str(changes.get("account_environment", "LIVE")),
        promotion=changes.get("promotion", _promotion()),  # pyright: ignore[reportArgumentType]
        binding_id=changes.get("binding_id", BINDING),
        composition_wired=bool(changes.get("composition_wired", True)),
        today=TODAY,
    )


def test_everything_in_place_would_allow_it() -> None:
    """A gate that could never open is a wall. This one opens when the state
    says it may - it just does not say so today."""
    assert _decide().allowed is True


def test_today_it_refuses_because_the_loop_is_not_wired() -> None:
    decision = _decide(composition_wired=False)
    assert decision.allowed is False
    assert NO_COMPOSITION in decision.reasons


def test_permission_and_mode_are_both_required() -> None:
    assert ALLOW_LIVE_ABSENT in _decide(settings=_settings(allow_live=False)).reasons
    assert (
        MODE_NOT_LIVE
        in _decide(settings=_settings(trading_mode=RuntimeMode.SHADOW)).reasons
    )


def test_a_live_run_against_a_paper_account_is_refused() -> None:
    """Real money behind a rehearsal is the direction that costs."""
    assert ACCOUNT_NOT_LIVE in _decide(account_environment="PAPER").reasons


def test_an_unpromoted_binding_is_refused_and_says_how_far() -> None:
    """§11.8's two Shadow and two Paper. What is left is what decides whether
    LIVE is close or far away, so it is in the message."""
    reasons = _decide(promotion=_promotion(ready=False)).reasons
    named = [item for item in reasons if item.startswith(NOT_PROMOTED)]
    assert named and "shadow 2 left" in named[0] and "paper 2 left" in named[0]


def test_a_binding_the_promotion_view_does_not_know_is_refused() -> None:
    assert BINDING_ABSENT in _decide(binding_id=uuid7()).reasons


def test_every_unmet_condition_is_named_at_once() -> None:
    """An operator who fixes one and is told about the next learns the list
    one restart at a time."""
    decision = _decide(
        settings=_settings(allow_live=False, trading_mode=RuntimeMode.SHADOW),
        account_environment="PAPER",
        promotion=_promotion(ready=False),
        composition_wired=False,
    )
    assert decision.allowed is False
    assert len(decision.reasons) == 5
    assert ALLOW_LIVE_ABSENT in decision.reasons
    assert MODE_NOT_LIVE in decision.reasons
    assert ACCOUNT_NOT_LIVE in decision.reasons
    assert NO_COMPOSITION in decision.reasons


def test_the_reasons_are_stable_to_read() -> None:
    first = _decide(composition_wired=False)
    assert first.reasons == replace(first).reasons


@pytest.mark.parametrize("mode", (RuntimeMode.SHADOW, RuntimeMode.PAPER))
def test_no_mode_but_live_starts_a_live_run(mode: RuntimeMode) -> None:
    assert MODE_NOT_LIVE in _decide(settings=_settings(trading_mode=mode)).reasons
