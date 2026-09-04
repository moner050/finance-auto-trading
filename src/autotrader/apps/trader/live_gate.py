"""Whether a LIVE run may start at all, and every reason it may not.

Section 11.8 puts LIVE behind two Shadow and two Paper sessions, and §22.9
behind promotion gates none of which have been passed. So this refuses, and
the refusal is the point rather than a placeholder: it reads the same
promotion state the backoffice shows and says no while that state says no.

Two things it does deliberately.

**It names every unmet condition, not the first.** An operator who fixes one
and is told about the next learns the list one restart at a time, and the
list is what decides whether LIVE is close or far away.

**It is a start-up gate, not the only one.** `SubmissionGate` still refuses
every individual write on `allow_live` and the runtime mode, and the loop
still refuses to act while disarmed. This one exists so that a run which
could never place an order does not spend a day pretending to.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from autotrader.apps.backoffice.promotion_read_model import PromotionView
from autotrader.config.settings import RuntimeMode, Settings

ALLOW_LIVE_ABSENT = "AUTOTRADER_ALLOW_LIVE is not set"
MODE_NOT_LIVE = "AUTOTRADER_TRADING_MODE is not LIVE"
ACCOUNT_NOT_LIVE = "the account is not registered as a LIVE account"
BINDING_ABSENT = "this account has no provider binding to promote"
NOT_PROMOTED = "the binding has not completed two Shadow and two Paper sessions"
NO_COMPOSITION = "the LIVE loop is not wired yet"


@dataclass(frozen=True, slots=True)
class LiveStartDecision:
    allowed: bool
    reasons: tuple[str, ...]


def may_start_live(
    *,
    settings: Settings,
    account_environment: str,
    promotion: PromotionView,
    binding_id: object,
    composition_wired: bool,
    today: date,
) -> LiveStartDecision:
    """Every reason this run may not place a live order, or none."""
    del today  # The promotion view was already loaded for it.
    reasons: list[str] = []
    if not settings.allow_live:
        reasons.append(ALLOW_LIVE_ABSENT)
    if settings.trading_mode is not RuntimeMode.LIVE:
        reasons.append(MODE_NOT_LIVE)
    if account_environment != RuntimeMode.LIVE.value:
        # A LIVE run against an account registered as PAPER would put real
        # money behind a rehearsal.
        reasons.append(ACCOUNT_NOT_LIVE)

    progress = next(
        (item for item in promotion.bindings if item.binding_id == binding_id),
        None,
    )
    if progress is None:
        reasons.append(BINDING_ABSENT)
    elif not progress.ready:
        reasons.append(
            f"{NOT_PROMOTED} "
            f"(shadow {progress.shadow_remaining} left, "
            f"paper {progress.paper_remaining} left)"
        )
    if not composition_wired:
        # Named rather than crashed into. A run that got this far and then
        # failed on an import would look like a bug rather than a state.
        reasons.append(NO_COMPOSITION)
    return LiveStartDecision(allowed=not reasons, reasons=tuple(reasons))


__all__ = (
    "ACCOUNT_NOT_LIVE",
    "ALLOW_LIVE_ABSENT",
    "BINDING_ABSENT",
    "MODE_NOT_LIVE",
    "NOT_PROMOTED",
    "NO_COMPOSITION",
    "LiveStartDecision",
    "may_start_live",
)
