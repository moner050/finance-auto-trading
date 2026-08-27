"""What the back office may arm.

`load_active_policy` builds its snapshot from the approved definition and
refuses a row that disagrees with it. A screen that can flag such a row active
does not produce a differently sized trade; it produces a loop that will not
start. This predicate lets the refusal happen at the form instead.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from autotrader.persistence.mysql.models.risk import RiskPolicyVersion
from autotrader.persistence.mysql.repositories.david_v6_risk import (
    APPROVED_V6_RISK_POLICIES,
    policy_row_refusal,
)
from autotrader.shared.ids import new_uuid7

KRW = next(
    item for item in APPROVED_V6_RISK_POLICIES if item.code == "DAVID_V6_CASH_KRW"
)


def _row(**changes: object) -> RiskPolicyVersion:
    values: dict[str, object] = {
        "id": new_uuid7(),
        "policy_id": new_uuid7(),
        "version": KRW.version,
        "active": False,
        "normal_risk_fraction": KRW.normal_risk_fraction,
        "a_candidate_risk_fraction": KRW.a_candidate_risk_fraction,
        "a_risk_fraction": KRW.a_risk_fraction,
        "absolute_trade_risk_fraction": KRW.absolute_trade_risk_fraction,
        "daily_loss_fraction": KRW.daily_loss_fraction,
        "weekly_loss_fraction": KRW.weekly_loss_fraction,
        "max_consecutive_losses": KRW.max_consecutive_losses,
        "max_open_structural_risk_fraction": KRW.max_open_structural_risk_fraction,
        "account_age_seconds": KRW.account_age_seconds,
        "risk_age_seconds": KRW.risk_age_seconds,
        "quote_age_seconds": KRW.quote_age_seconds,
        "provider_age_seconds": KRW.provider_age_seconds,
        "stream_gap_age_seconds": KRW.stream_gap_age_seconds,
        "completed_intraday_bar_arrival_seconds": (
            KRW.completed_intraday_bar_arrival_seconds
        ),
        "daily_requires_authoritative_close": KRW.daily_requires_authoritative_close,
    }
    values.update(changes)
    return RiskPolicyVersion(**values)


def test_the_approved_row_is_loadable() -> None:
    assert policy_row_refusal(_row(), code=KRW.code) is None


def test_a_row_whose_numbers_moved_is_not() -> None:
    row = _row(normal_risk_fraction=Decimal("0.0010"))

    assert policy_row_refusal(row, code=KRW.code) is not None


def test_a_row_at_a_version_the_loop_does_not_look_for_is_not() -> None:
    # `load_active_policy` matches on the version string, so a row under a
    # different one is never even read.
    row = _row(version="v6-op-20261231.9")

    assert policy_row_refusal(row, code=KRW.code) is not None


def test_an_unapproved_code_is_not() -> None:
    assert policy_row_refusal(_row(), code="DAVID_V6_SOMETHING_ELSE") is not None


def test_an_absolute_cap_on_a_percentage_policy_is_not() -> None:
    row = _row(max_total_risk=Decimal("1000000"))

    assert policy_row_refusal(row, code=KRW.code) is not None


@pytest.mark.parametrize("definition", APPROVED_V6_RISK_POLICIES)
def test_every_approved_definition_describes_a_loadable_row(definition: object) -> None:
    """Otherwise there would be a market with no version anyone can arm."""
    assert isinstance(definition, type(KRW))
    row = _row(
        version=definition.version,
        normal_risk_fraction=definition.normal_risk_fraction,
        a_candidate_risk_fraction=definition.a_candidate_risk_fraction,
        a_risk_fraction=definition.a_risk_fraction,
        absolute_trade_risk_fraction=definition.absolute_trade_risk_fraction,
        daily_loss_fraction=definition.daily_loss_fraction,
        weekly_loss_fraction=definition.weekly_loss_fraction,
        max_consecutive_losses=definition.max_consecutive_losses,
        max_open_structural_risk_fraction=(
            definition.max_open_structural_risk_fraction
        ),
        account_age_seconds=definition.account_age_seconds,
        risk_age_seconds=definition.risk_age_seconds,
        quote_age_seconds=definition.quote_age_seconds,
        provider_age_seconds=definition.provider_age_seconds,
        stream_gap_age_seconds=definition.stream_gap_age_seconds,
        completed_intraday_bar_arrival_seconds=(
            definition.completed_intraday_bar_arrival_seconds
        ),
        daily_requires_authoritative_close=(
            definition.daily_requires_authoritative_close
        ),
    )

    assert policy_row_refusal(row, code=definition.code) is None
