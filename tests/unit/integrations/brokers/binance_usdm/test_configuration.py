from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from autotrader.integrations.brokers.binance_usdm.account import (
    BinanceUsdmAccountSnapshot,
    BinanceUsdmBalance,
    BinanceUsdmPosition,
)
from autotrader.integrations.brokers.binance_usdm.configuration import (
    BinanceUsdmApiKeyEvidence,
    verify_binance_usdm_configuration,
)
from autotrader.integrations.brokers.common import BrokerRequest, BrokerResponse

AS_OF = datetime(2026, 8, 24, 15, 30, tzinfo=UTC)


def _response(payload: object) -> BrokerResponse:
    return BrokerResponse(200, json.dumps(payload).encode())


@dataclass
class _Reader:
    leverage: int = 7
    margin_type: str = "ISOLATED"
    auto_add: bool = False
    dual_side: bool = False
    multi_asset: bool = False
    requests: list[BrokerRequest] = field(default_factory=lambda: [])

    async def send(self, request: BrokerRequest) -> BrokerResponse:
        self.requests.append(request)
        path = request.path.partition("?")[0]
        if path == "/fapi/v1/accountConfig":
            return _response(
                {
                    "canTrade": True,
                    "canDeposit": True,
                    "canWithdraw": True,
                    "dualSidePosition": self.dual_side,
                    "multiAssetsMargin": self.multi_asset,
                }
            )
        if path == "/fapi/v1/positionSide/dual":
            return _response({"dualSidePosition": self.dual_side})
        if path == "/fapi/v1/symbolConfig":
            return _response(
                [
                    {
                        "symbol": "BTCUSDT",
                        "marginType": self.margin_type,
                        "isAutoAddMargin": self.auto_add,
                        "leverage": self.leverage,
                        "maxNotionalValue": "1000000",
                    }
                ]
            )
        raise AssertionError(path)


def _snapshot(
    *,
    positions: tuple[BinanceUsdmPosition, ...] = (),
) -> BinanceUsdmAccountSnapshot:
    return BinanceUsdmAccountSnapshot(
        as_of=AS_OF,
        balances=(
            BinanceUsdmBalance(
                asset="USDT",
                balance=Decimal("1000"),
                available_balance=Decimal("1000"),
                maximum_withdraw_amount=Decimal("1000"),
                updated_at=AS_OF,
            ),
        ),
        positions=positions,
        normal_orders=(),
        algo_orders=(),
        trades=(),
        income=(),
    )


def _position(symbol: str, amount: str) -> BinanceUsdmPosition:
    return BinanceUsdmPosition(
        symbol=symbol,
        position_side="BOTH",
        amount=Decimal(amount),
        entry_price=Decimal("60000"),
        mark_price=Decimal("60000"),
        unrealized_pnl=Decimal("0"),
        isolated_margin=Decimal("0"),
        notional=Decimal("0"),
        margin_asset="USDT",
        initial_margin=Decimal("0"),
        maintenance_margin=Decimal("0"),
        position_initial_margin=Decimal("0"),
        open_order_initial_margin=Decimal("0"),
        updated_at=AS_OF,
    )


def _key_evidence(
    *,
    ip_restricted: bool | None = True,
    withdrawals_enabled: bool | None = False,
) -> BinanceUsdmApiKeyEvidence:
    return BinanceUsdmApiKeyEvidence(
        captured_at=AS_OF - timedelta(minutes=1),
        api_key_fingerprint=b"k" * 32,
        ip_restricted=ip_restricted,
        withdrawals_enabled=withdrawals_enabled,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("leverage", (1, 7))
async def test_exact_one_way_isolated_auto_add_off_and_leverage_are_ready(
    leverage: int,
) -> None:
    reader = _Reader(leverage=leverage)

    report = await verify_binance_usdm_configuration(
        reader=reader,
        snapshot=_snapshot(),
        api_key_evidence=_key_evidence(),
        expected_leverage=leverage,
        as_of=AS_OF,
    )

    assert report.ready is True
    assert report.blockers == ()
    assert report.position_mode == "ONE_WAY"
    assert report.margin_type == "ISOLATED"
    assert report.auto_add_margin is False
    assert report.leverage == leverage
    assert report.can_trade is True
    assert report.multi_assets_margin is False
    assert report.account_transfer_out_enabled is True
    assert [request.method for request in reader.requests] == ["GET", "GET", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reader", "blocker"),
    (
        (_Reader(dual_side=True), "POSITION_MODE_NOT_ONE_WAY"),
        (_Reader(margin_type="CROSSED"), "MARGIN_TYPE_NOT_ISOLATED"),
        (_Reader(auto_add=True), "AUTO_ADD_MARGIN_ENABLED"),
        (_Reader(leverage=0), "LEVERAGE_OUT_OF_RANGE"),
        (_Reader(leverage=8), "LEVERAGE_OUT_OF_RANGE"),
    ),
)
async def test_unsafe_provider_configuration_is_blocked(
    reader: _Reader,
    blocker: str,
) -> None:
    report = await verify_binance_usdm_configuration(
        reader=reader,
        snapshot=_snapshot(),
        api_key_evidence=_key_evidence(),
        expected_leverage=7,
        as_of=AS_OF,
    )

    assert report.ready is False
    assert blocker in report.blockers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("evidence", "blocker"),
    (
        (_key_evidence(ip_restricted=False), "API_KEY_IP_NOT_RESTRICTED"),
        (_key_evidence(ip_restricted=None), "API_KEY_IP_RESTRICTION_UNPROVEN"),
        (_key_evidence(withdrawals_enabled=True), "API_KEY_WITHDRAWALS_ENABLED"),
        (_key_evidence(withdrawals_enabled=None), "API_KEY_WITHDRAWALS_UNPROVEN"),
    ),
)
async def test_api_key_authority_evidence_fails_closed(
    evidence: BinanceUsdmApiKeyEvidence,
    blocker: str,
) -> None:
    report = await verify_binance_usdm_configuration(
        reader=_Reader(),
        snapshot=_snapshot(),
        api_key_evidence=evidence,
        expected_leverage=7,
        as_of=AS_OF,
    )

    assert blocker in report.blockers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("positions", "blocker"),
    (
        ((_position("BTCUSDT", "0.01"),), "UNOWNED_BTCUSDT_EXPOSURE"),
        ((_position("ETHUSDT", "1"),), "UNEXPECTED_SYMBOL_EXPOSURE"),
    ),
)
async def test_unowned_or_unexpected_exposure_is_blocked(
    positions: tuple[BinanceUsdmPosition, ...],
    blocker: str,
) -> None:
    report = await verify_binance_usdm_configuration(
        reader=_Reader(),
        snapshot=_snapshot(positions=positions),
        api_key_evidence=_key_evidence(),
        expected_leverage=7,
        as_of=AS_OF,
    )

    assert blocker in report.blockers


@pytest.mark.asyncio
@pytest.mark.parametrize("expected_leverage", (0, 8))
async def test_invalid_expected_leverage_fails_before_any_read(
    expected_leverage: int,
) -> None:
    reader = _Reader()

    with pytest.raises(ValueError, match="leverage"):
        await verify_binance_usdm_configuration(
            reader=reader,
            snapshot=_snapshot(),
            api_key_evidence=_key_evidence(),
            expected_leverage=expected_leverage,
            as_of=AS_OF,
        )

    assert reader.requests == []


@pytest.mark.asyncio
async def test_stale_key_evidence_and_mismatched_snapshot_are_blocked() -> None:
    stale = replace(
        _key_evidence(),
        captured_at=AS_OF - timedelta(days=1, seconds=1),
    )

    report = await verify_binance_usdm_configuration(
        reader=_Reader(),
        snapshot=replace(_snapshot(), as_of=AS_OF - timedelta(seconds=1)),
        api_key_evidence=stale,
        expected_leverage=7,
        as_of=AS_OF,
    )

    assert "ACCOUNT_SNAPSHOT_TIME_MISMATCH" in report.blockers
    assert "API_KEY_EVIDENCE_STALE" in report.blockers
