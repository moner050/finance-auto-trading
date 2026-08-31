"""Starting the trader, and being told why it will not start.

The value of this program today is the refusal. Six of the loop's inputs have
no producer, so the tests that matter are the ones checking that it names them
rather than inventing them, and that it refuses one at a time as an operator
fills in the back office.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid7

import pytest
from conftest import integration_database_url
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotrader.apps.trader.startup import (
    UNSOURCED_INPUTS,
    VENUE_SOURCED,
    OperatorFacts,
    StartupRefusedError,
    resolve_account,
    unsourced_inputs,
)
from autotrader.config.settings import Settings
from autotrader.persistence.mysql.engine import create_engine
from autotrader.persistence.mysql.models.accounts import Account, Broker
from autotrader.persistence.mysql.models.bindings import ProviderAccountBinding
from autotrader.persistence.mysql.seeds.core import (
    BINANCE_USDM_EXCHANGE_CODE,
    seed_core_reference_session,
)
from autotrader.strategies.david_v6.models import V6Market

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
ALIAS = "startup-account"


def _drive(scenario: object) -> None:
    url = integration_database_url()
    if url is None:
        pytest.skip("MySQL is required for acceptance tests")

    async def run() -> None:
        engine = create_engine(Settings(database_url=url))
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            await scenario(sessions)  # type: ignore[operator]
        finally:
            await engine.dispose()

    asyncio.run(run())


async def _account(
    sessions: async_sessionmaker[AsyncSession], *, enabled: bool = False
) -> UUID:
    async with sessions() as session:
        broker = await session.scalar(select(Broker).where(Broker.code == "BINANCE"))
        if broker is None:
            broker = Broker(id=uuid7(), code="BINANCE", name="Binance")
            session.add(broker)
            await session.flush()
        account = Account(
            id=uuid7(),
            broker_id=broker.id,
            account_alias=ALIAS,
            environment="LIVE",
            secret_reference="secret://none",
            enabled=enabled,
        )
        session.add(account)
        await session.commit()
        return account.id


async def _bind_provider(
    sessions: async_sessionmaker[AsyncSession], account_id: UUID
) -> None:
    async with sessions() as session:
        account = await session.scalar(select(Account).where(Account.id == account_id))
        assert account is not None
        session.add(
            ProviderAccountBinding(
                id=uuid7(),
                account_id=account_id,
                broker_id=account.broker_id,
                provider_code="BINANCE",
                environment="LIVE",
                account_seq=None,
                revision=1,
                observed_at=NOW - timedelta(days=1),
                active=True,
            )
        )
        await session.commit()


async def _resolve(sessions: async_sessionmaker[AsyncSession]) -> object:
    return await resolve_account(
        sessions,
        account_alias=ALIAS,
        market=V6Market.BINANCE_USDM,
        instrument_code="BTCUSDT",
        exchange_code=BINANCE_USDM_EXCHANGE_CODE,
    )


def _names(error: StartupRefusedError) -> set[str]:
    return {item.name for item in error.missing}


@pytest.mark.acceptance
@pytest.mark.integration
def test_an_unknown_alias_is_refused_by_name() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        with pytest.raises(StartupRefusedError) as caught:
            await _resolve(sessions)

        assert _names(caught.value) == {"account"}
        assert ALIAS in str(caught.value)

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_disabled_account_is_one_of_the_reasons() -> None:
    """Enabling is what the accounts screen gates behind the second password,
    so the loop naming it points at the step that was skipped."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        await _account(sessions, enabled=False)

        with pytest.raises(StartupRefusedError) as caught:
            await _resolve(sessions)

        assert "account.enabled" in _names(caught.value)

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_every_missing_thing_is_named_at_once() -> None:
    """An operator who fixes one and is told about the next is being walked
    through the list one restart at a time."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        await _account(sessions, enabled=False)

        with pytest.raises(StartupRefusedError) as caught:
            await _resolve(sessions)

        # Disabled, unbound to a provider, unbound to a policy, no manifest,
        # and no registered instrument — all of it, in one refusal.
        assert len(caught.value.missing) >= 4

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_binding_a_provider_removes_that_reason_and_no_other() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        account_id = await _account(sessions, enabled=True)

        with pytest.raises(StartupRefusedError) as before:
            await _resolve(sessions)
        assert "provider_binding" in _names(before.value)

        await _bind_provider(sessions, account_id)

        with pytest.raises(StartupRefusedError) as after:
            await _resolve(sessions)
        assert "provider_binding" not in _names(after.value)
        # The rest are still missing; fixing one thing fixes one thing.
        assert _names(after.value) == _names(before.value) - {"provider_binding"}

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_an_unseeded_exchange_is_a_reason_before_the_instrument_is() -> None:
    """A missing reference seed is a missing seed, not a crash. Letting it
    escape would defeat the point of collecting reasons."""

    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        await _account(sessions, enabled=True)

        with pytest.raises(StartupRefusedError) as caught:
            await _resolve(sessions)

        assert "exchange" in _names(caught.value)

    _drive(scenario)


@pytest.mark.acceptance
@pytest.mark.integration
def test_a_registered_instrument_stops_being_a_reason() -> None:
    async def scenario(sessions: async_sessionmaker[AsyncSession]) -> None:
        from autotrader.apps.trader.binance_paper import BTCUSDT
        from autotrader.persistence.mysql.repositories.core import (
            CoreInstrumentRegistry,
        )

        await _account(sessions, enabled=True)
        async with sessions() as session:
            await seed_core_reference_session(session)
            await session.commit()

        with pytest.raises(StartupRefusedError) as before:
            await _resolve(sessions)
        # The exchange exists now, so the instrument is what is missing.
        assert "exchange" not in _names(before.value)
        assert "instrument" in _names(before.value)

        async with sessions() as session:
            await CoreInstrumentRegistry(session).register(BTCUSDT)
            await session.commit()

        with pytest.raises(StartupRefusedError) as after:
            await _resolve(sessions)
        assert "instrument" not in _names(after.value)

    _drive(scenario)


def test_the_unsourced_inputs_are_named_rather_than_counted() -> None:
    """The list is the work item. A count would say the loop cannot run
    without saying what would make it able to."""
    missing = unsourced_inputs()

    assert missing == UNSOURCED_INPUTS
    assert {item.name for item in missing} == {"range_efficiency", "atr_ratio"}
    assert all(item.reason for item in missing)


def test_the_authors_own_regime_needs_nothing_chosen() -> None:
    """Section 2.1 gives it as SMA 6/70/200 on the instrument itself, and the
    trend is computed over a series rebuilt from returns, which a moving
    average cannot tell apart from the closes. So the benchmark was never a
    choice to make."""
    assert "benchmark_returns" not in {item.name for item in unsourced_inputs()}
    assert "benchmark_returns" in VENUE_SOURCED


def test_the_session_boundary_was_measured_rather_than_left_open() -> None:
    """A perpetual venue has no close, so one was placed where liquidity
    thins. It is a decision, but it is made."""
    assert "calendar" not in {item.name for item in unsourced_inputs()}
    assert "calendar" in VENUE_SOURCED


def test_the_fee_is_read_from_the_account_rather_than_a_rate_card() -> None:
    """A published rate card does not know this account's tier, referral or
    BNB setting, and a fee that is too low makes a cost filter agree with
    trades it has not actually priced."""
    assert "fee_schedule" not in {item.name for item in unsourced_inputs()}
    assert "fee_schedule" in VENUE_SOURCED


def test_the_put_call_percentile_is_no_longer_waited_on() -> None:
    """It cannot be backfilled - Deribit's public tape retains about a day and
    its volume endpoint is rolling - but section 2.3 marks the quantitative
    triple as curriculum and the detector the author used as a newspaper. Two
    measured components decide; this one joins them once it has a history."""
    assert "pessimism.put_call_percentile" not in {
        item.name for item in unsourced_inputs()
    }
    assert "pessimism.put_call_percentile" in VENUE_SOURCED


def test_telemetry_the_author_never_published_is_not_demanded() -> None:
    """Ceros osmóticos is undisclosed, confidence LOW, telemetry only, and no
    decision reads it. Requiring its thresholds would make an operator invent
    two numbers to compute a field nothing consults."""
    assert "order_flow_thresholds" not in {item.name for item in unsourced_inputs()}


def test_what_the_venue_answers_is_no_longer_missing() -> None:
    """Tick size and lot size come from exchangeInfo now, so they are facts
    rather than settings, and settings are where a wrong number hides."""
    names = {item.name for item in unsourced_inputs()}

    assert names.isdisjoint(VENUE_SOURCED)


def test_operator_facts_have_no_defaults() -> None:
    """Every field decides the size or price of a real order. A default here
    would be this module guessing at the operator's money."""
    with pytest.raises(TypeError):
        OperatorFacts()  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "field",
    ("session_start_equity", "current_equity", "quantity_step", "tick_size"),
)
def test_a_non_positive_operator_fact_is_refused(field: str) -> None:
    values: dict[str, object] = {
        "session_start_equity": Decimal("2000"),
        "current_equity": Decimal("2000"),
        "quantity_step": Decimal("0.001"),
        "tick_size": Decimal("0.1"),
        "spread": Decimal("0.1"),
        "cost_per_unit": Decimal("0.02"),
        "leverage": 3,
    }
    values[field] = Decimal("0")

    with pytest.raises(ValueError, match="positive"):
        OperatorFacts(**values)  # type: ignore[arg-type]
