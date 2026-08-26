from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from autotrader.integrations.brokers.paper_submitter import PaperAccount
from autotrader.strategies.david_v6.models import V6Market


def _account(alias: str, market: V6Market) -> PaperAccount:
    return PaperAccount(
        account_alias=alias,
        market=market,
        timeframe=timedelta(minutes=5),
        fee_per_unit=Decimal("0.01"),
        slippage_per_unit=Decimal("0.01"),
    )


def test_each_market_has_its_own_paper_account() -> None:
    for alias, market in (
        ("internal-us-paper", V6Market.US_CASH),
        ("internal-krx-paper", V6Market.KRX_CASH),
        ("internal-binance-usdm-paper", V6Market.BINANCE_USDM),
    ):
        assert _account(alias, market).account_alias == alias


def test_an_alias_from_another_market_is_refused() -> None:
    # Dispatch turns anything the broker raises into UNKNOWN, so a binding
    # checked only at submission time would read as a broker timeout. This
    # has to fail where the account is wired.
    with pytest.raises(ValueError, match="is not the paper account for"):
        _account("internal-krx-paper", V6Market.US_CASH)


def test_an_unknown_alias_is_refused() -> None:
    with pytest.raises(ValueError, match="is not the paper account for"):
        _account("acceptance-paper", V6Market.US_CASH)
